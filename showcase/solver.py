"""Answers quiz questions through one long-lived `claude` CLI process (uses the Claude Code login)."""

from __future__ import annotations

import base64
import json
import logging
import queue
import re
import shutil
import subprocess
import threading
import time

from config import CLAUDE_TIMEOUT_S, TYPED_ANSWER_MAX_CHARS

logger = logging.getLogger("kahoot_bot")

SYSTEM_PROMPT = (
    "You are playing a live quiz game. Each message is one question, given as text, as a screenshot "
    "of the quiz screen, or both. Answer from your own knowledge as accurately as you can; if you are "
    "unsure, still give your single best guess. A program parses your reply, so the format is strict: "
    "one line of the form `ANSWER | CONFIDENCE`. ANSWER is the number of the correct option for choice "
    "questions (several numbers separated by commas when the question says more than one may be "
    "correct) or the short answer text for type-answer questions. CONFIDENCE is an integer from 0 to "
    "100: your honest probability that ANSWER is right. The program uses it to decide whether to spend "
    "more time on the question, so do not round it up. Never add explanations or any other words."
)

# First turn of every process: it pays the start-up cost so that the first question does not.
WARM_UP_PROMPT = "Reply with the single word: ready"

# What the model replies instead of an answer when a screenshot shows no quiz question.
NO_QUESTION_MARK = "NONE"
NO_QUESTION_RULE = (
    f"If the screenshot shows no quiz question at all, reply exactly `{NO_QUESTION_MARK} | 0` instead of guessing."
)


class ClaudeSolver:
    """Keeps `claude -p` alive in stream-json mode so each question costs only model latency."""

    def __init__(self, label: str, model: str, effort: str) -> None:
        self._label = label  # only tells the processes apart in the debug log
        self._model = model
        self._effort = effort
        self._process: subprocess.Popen[str] | None = None
        self._events: queue.Queue[dict | None] = queue.Queue()
        self._stale_results = 0
        self._asked = 0.0

    def start(self) -> None:
        """Launches the process and sends the warm-up turn without waiting for it; see wait_ready."""
        executable = shutil.which("claude")
        if executable is None:
            raise RuntimeError("`claude` komutu PATH üzerinde bulunamadı.")
        self._process = subprocess.Popen(
            [
                executable, "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose",
                "--model", self._model,
                "--effort", self._effort,
                "--tools", "",
                # safe-mode skips the user's hooks, plugins and CLAUDE.md, which would slow every turn
                "--safe-mode",
                "--strict-mcp-config",
                "--no-session-persistence",
                "--system-prompt", SYSTEM_PROMPT,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            # no console window when the bot is started from the windowless launcher
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._read_events, daemon=True).start()
        self._send_prompt(WARM_UP_PROMPT, None)

    def wait_ready(self, timeout: float = 60.0) -> None:
        """Blocks until the warm-up turn has finished; raises like `ask` when the process is unusable."""
        self._collect(timeout)

    def ask(self, text: str, image_jpeg: bytes | None = None, timeout: float = CLAUDE_TIMEOUT_S) -> str:
        self._send_prompt(text, image_jpeg)
        return self._collect(timeout)

    def _send_prompt(self, text: str, image_jpeg: bytes | None) -> None:
        content: list[dict] = []
        if image_jpeg is not None:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(image_jpeg).decode("ascii"),
                },
            })
        content.append({"type": "text", "text": text})
        logger.debug("[%s] ask (image %d bytes):\n%s", self._label, len(image_jpeg or b""), text)
        self._send({"type": "user", "message": {"role": "user", "content": content}})
        self._asked = time.monotonic()

    def _collect(self, timeout: float) -> str:
        """The reply to the prompt sent last; TimeoutError when it does not arrive within `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                event = self._events.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                # The late reply will still arrive; it must not be mistaken for the next question's.
                self._stale_results += 1
                logger.debug("[%s] no reply within %.1f s", self._label, timeout)
                raise TimeoutError(f"Claude {timeout:.0f} sn içinde cevap vermedi.") from None
            if event is None:
                raise RuntimeError("claude süreci kapandı (giriş yapılmış mı? `claude` ile kontrol et).")
            if event.get("type") != "result":
                continue
            if self._stale_results:
                self._stale_results -= 1
                continue
            if event.get("is_error"):
                raise RuntimeError(f"Claude hata döndürdü: {event.get('result')}")
            reply = str(event.get("result", "")).strip()
            logger.debug("[%s] reply after %.2f s: %r", self._label, time.monotonic() - self._asked, reply)
            return reply

    def close(self) -> None:
        if self._process is None:
            return
        try:
            self._process.stdin.close()
            self._process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self._process.kill()
        self._process = None

    def _send(self, message: dict) -> None:
        if self._process is None or self._process.poll() is not None:
            raise RuntimeError("claude süreci çalışmıyor.")
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def _read_events(self) -> None:
        for line in self._process.stdout:
            try:
                self._events.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self._events.put(None)


def build_prompt(question: dict, image_attached: bool) -> str:
    """`question` is the snapshot produced by KahootSession.wait_for_event."""
    title: str = question["title"]
    if question["typed"]:
        limit = f"The answer text may be {TYPED_ANSWER_MAX_CHARS} characters at most."
        if title:
            return f"Type-answer question: {title}\n{limit}"
        return f"The screenshot shows a type-answer quiz question.\n{limit}\n{NO_QUESTION_RULE}"

    choices: list[dict] = question["choices"]
    rule = (
        "More than one option may be correct; reply with every correct number, separated by commas."
        if question["multi"]
        else "Exactly one option is correct; reply with only its number."
    )
    if title and all(choice["text"] for choice in choices):
        lines = [f"Question: {title}"]
        lines += [f"{number}. {choice['text']}" for number, choice in enumerate(choices, start=1)]
        if image_attached:
            lines.append("The attached image shows the picture that accompanies the question.")
        return "\n".join(lines + [rule])

    lines = [
        f"The screenshot shows a quiz question with {len(choices)} answer options.",
        "If several windows are visible, use the one that shows the question text and the labelled options.",
        f"Number the options 1 to {len(choices)} in reading order (left to right, then top to bottom).",
    ]
    if title:
        lines.append(f"Question text: {title}")
    return "\n".join(lines + [rule, NO_QUESTION_RULE])


def says_no_question(reply: str) -> bool:
    return reply.strip().upper().startswith(NO_QUESTION_MARK)


def interpret_reply(reply: str, question: dict) -> tuple[list[int] | str | None, int | None]:
    """Splits `ANSWER | CONFIDENCE` into a usable answer and a 0-100 confidence.

    The answer is a list of 0-based option indexes, or the text for type-answer questions;
    None when the reply cannot be used. Confidence is None when the model left it out.
    """
    answer_text, separator, tail = reply.rpartition("|")
    confidence: int | None = None
    if separator:
        digits = re.search(r"\d+", tail)
        confidence = min(100, int(digits.group())) if digits else None
    else:
        answer_text = reply
    answer_text = answer_text.strip()

    if question["typed"]:
        return (answer_text[:TYPED_ANSWER_MAX_CHARS] or None), confidence

    indexes: list[int] = []
    for match in re.findall(r"\d+", answer_text):
        index = int(match) - 1
        if 0 <= index < len(question["choices"]) and index not in indexes:
            indexes.append(index)
    if not question["multi"]:
        indexes = indexes[:1]
    return (indexes or None), confidence
