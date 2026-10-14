"""Quiz content from Kahoot's public endpoints, so the bot can read questions without the host screen.

A quiz is either named by the user (id or link) or identified the way a student-side tool would:
the game-start message every player receives carries the full first question, and the public
search finds the kahoot that contains it.

Only question text, choice text and pictures are used. The answer key that the same payloads
carry is deliberately ignored: the bot's job is to answer like an AI player, not to look answers up.
"""

from __future__ import annotations

import html
import io
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from PIL import Image

logger = logging.getLogger("kahoot_bot")

QUIZ_API_URL = "https://create.kahoot.it/rest/kahoots/{}"
SEARCH_API_URL = (
    "https://create.kahoot.it/rest/kahoots/?query={}&limit=25&orderBy=relevance"
    "&searchCluster=1&includeExtendedCounters=false"
)
QUIZ_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
REQUEST_TIMEOUT_S = 15
IMAGE_MAX_SIDE_PX = 800
# Search hits that are downloaded and compared with the first question before giving up.
MAX_SEARCH_CANDIDATES = 6

CHOICE_KINDS = ("quiz", "multiple_select_quiz")
TYPED_KIND = "open_ended"


@dataclass
class QuizQuestion:
    kind: str
    title: str
    choices: list[str]
    image_url: str
    image_id: str
    image_jpeg: bytes | None = None


@dataclass
class Quiz:
    quiz_id: str
    title: str
    blocks: list[QuizQuestion]  # one per game block, in quiz order


def parse_quiz_id(value: str) -> str:
    """Accepts a bare quiz id or any Kahoot link that contains one."""
    match = QUIZ_ID_PATTERN.search(value)
    if match is None:
        raise ValueError(f"'{value}' içinde quiz ID'si bulunamadı (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx biçiminde olmalı).")
    return match.group().lower()


def block_from_payload(block: dict) -> QuizQuestion:
    """Works for a block of the quiz endpoint and for the first question in the game-start message."""
    return QuizQuestion(
        kind=block.get("type", ""),
        title=_plain_text(block.get("question") or block.get("title") or ""),
        choices=[_plain_text(choice.get("answer") or "") for choice in block.get("choices") or []],
        image_url=block.get("image") or "",
        image_id=(block.get("imageMetadata") or {}).get("id") or "",
    )


def fetch_quiz(quiz_id: str) -> Quiz:
    try:
        payload = json.loads(_download(QUIZ_API_URL.format(quiz_id)))
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"Quiz içeriği alınamadı (HTTP {error.code}). Quiz özel olabilir; herkese açık yap "
            "ya da host ayarlarından 'Soruları cihazlarda göster'i aç."
        ) from None
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Quiz içeriği alınamadı: {error}") from None
    blocks = [block_from_payload(block) for block in payload.get("questions", [])]
    return Quiz(quiz_id, payload.get("title", ""), blocks)


def find_quiz(first: QuizQuestion, total_blocks: int | None) -> Quiz | None:
    """Searches public kahoots for the one that contains `first`; None when no hit matches."""
    if not first.title:
        return None
    hits = json.loads(_download(SEARCH_API_URL.format(urllib.parse.quote(first.title)))).get("entities", [])
    candidate_ids = [
        hit["card"]["uuid"] for hit in hits
        if total_blocks is None or hit["card"].get("number_of_questions") == total_blocks
    ][:MAX_SEARCH_CANDIDATES]
    logger.info("quiz search for %r: %d hits, checking %s", first.title, len(hits), candidate_ids)

    with ThreadPoolExecutor(max_workers=MAX_SEARCH_CANDIDATES) as pool:
        for quiz in pool.map(_fetch_or_none, candidate_ids):
            if quiz is None or (total_blocks is not None and len(quiz.blocks) != total_blocks):
                continue
            if any(_same_question(first, block) for block in quiz.blocks):
                return quiz
    return None


def load_images(blocks: list[QuizQuestion]) -> None:
    """Pictures can take tens of seconds on a slow line, so they arrive in the background;
    a question asked before its picture is ready simply goes out as text only."""
    threading.Thread(target=_load_images, args=(blocks,), daemon=True).start()


def pick_block(quiz: Quiz, index: int, image_id: str | None, shuffled: bool) -> QuizQuestion | None:
    """The quiz block being played as block number `index` (0-based).

    The server announces each upcoming block's picture id, which pins the block down even when the
    host shuffles question order; without a usable picture id only an unshuffled game can be mapped.
    """
    if image_id:
        matches = [block for block in quiz.blocks if block.image_id == image_id]
        if len(matches) == 1:
            return matches[0]
    if not shuffled and 0 <= index < len(quiz.blocks):
        return quiz.blocks[index]
    return None


def apply_block(block: QuizQuestion, question: dict) -> tuple[dict, bytes | None] | None:
    """Fills the live question's missing text from `block`; None when the two do not line up."""
    if question["typed"]:
        if block.kind != TYPED_KIND or not block.title:
            return None
        return {**question, "title": block.title}, block.image_jpeg

    # The selector number is the choice's original index, so this also holds when answers are shuffled.
    original_indexes = [int(choice["selector"].rsplit("-", 1)[1]) for choice in question["choices"]]
    lines_up = (
        block.kind in CHOICE_KINDS
        and block.title
        and len(block.choices) == len(original_indexes)
        and all(index < len(block.choices) and block.choices[index] for index in original_indexes)
    )
    if not lines_up:
        return None
    choices = [
        {**choice, "text": block.choices[index]} for choice, index in zip(question["choices"], original_indexes)
    ]
    return {**question, "title": block.title, "choices": choices}, block.image_jpeg


def _fetch_or_none(quiz_id: str) -> Quiz | None:
    try:
        return fetch_quiz(quiz_id)
    except RuntimeError as error:
        logger.info("candidate %s skipped: %s", quiz_id, error)
        return None


def _same_question(first: QuizQuestion, other: QuizQuestion) -> bool:
    return (
        _normalized(first.title) == _normalized(other.title)
        and sorted(map(_normalized, first.choices)) == sorted(map(_normalized, other.choices))
    )


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        return response.read()


def _load_images(blocks: list[QuizQuestion]) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        for block, image in zip(blocks, pool.map(_load_image, (block.image_url for block in blocks))):
            block.image_jpeg = image
    logger.info("pictures loaded: %d of %d blocks", sum(b.image_jpeg is not None for b in blocks), len(blocks))


def _load_image(url: str) -> bytes | None:
    if not url:
        return None
    try:
        # Kahoot's media server scales and re-encodes on request: a 950 KB PNG arrives as a 14 KB JPEG.
        image = Image.open(io.BytesIO(_download(f"{url}?width={IMAGE_MAX_SIDE_PX}&format=jpg"))).convert("RGB")
    except (OSError, ValueError) as error:
        logger.warning("question image could not be loaded: %s (%s)", url, error)
        return None
    image.thumbnail((IMAGE_MAX_SIDE_PX, IMAGE_MAX_SIDE_PX))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


def _plain_text(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup)).strip()
