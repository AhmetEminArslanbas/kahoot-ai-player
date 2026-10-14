"""Drives the Kahoot player page in a real browser: join, detect questions, submit answers."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from playwright.sync_api import Browser, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from config import (
    CLICK_TIMEOUT_MS,
    JOIN_TIMEOUT_MS,
    MULTI_SUBMIT,
    NAME_INPUT,
    NAME_SUBMIT,
    PIN_INPUT,
    PIN_SUBMIT,
    TEXT_ANSWER_INPUT,
    TWO_FACTOR_PAGE,
    sel,
)

logger = logging.getLogger("kahoot_bot")

# Channel and message ids of Kahoot's player websocket feed (seen in a live game, 2026-10-06).
PLAYER_CHANNEL = "/service/player"
MESSAGE_GET_READY = 1
MESSAGE_QUESTION_START = 2
MESSAGE_QUESTION_RESULT = 8
MESSAGE_GAME_START = 9
MESSAGE_GAME_OVER = 13

# Without these Chromium throttles timers in a window hidden behind the host screen,
# which would delay question detection by up to a second.
BROWSER_ARGS = [
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-features=CalculateNativeWinOcclusion",
]

# Resolves on the next thing the bot must react to: an answerable question, the result of the
# question it just answered, or the end of the game.
# Answer buttons are returned in DOM order, which is the on-screen reading order even when
# the host shuffles answers (the selector then carries the original index, not the position).
NEXT_EVENT_JS = r"""
(state) => {
  const one = (name) => document.querySelector(`[data-functional-selector="${name}"]`);
  const text = (el) => (el ? el.innerText.trim() : "");

  const gameOverHeader = document.querySelector('[data-functional-selector^="game-over-header"]');
  if (gameOverHeader || /\/(ranking|gameover|feedback)$/.test(location.pathname)) {
    const main = document.querySelector("main") || document.body;
    return { state: "over", text: main.innerText.replace(/\s+/g, " ").trim().slice(0, 160) };
  }

  if (state.awaitingResult) {
    const outcomes = [
      ["correct-answer", "correct"],
      ["partial-correct-answer", "partial"],
      ["almost-correct-answer", "partial"],
      ["incorrect-answer", "incorrect"],
      ["multi-incorrect-answer", "incorrect"],
      ["time-up-answer", "timeout"],
    ];
    for (const [name, outcome] of outcomes) {
      if (one(name)) return { state: "result", outcome, points: text(one("score-increment")) };
    }
  }

  const buttons = [...document.querySelectorAll('[data-functional-selector^="answer-"]')]
    .filter((el) => /^answer-\d+$/.test(el.dataset.functionalSelector));
  const input = one("text-answer-input");
  if (!buttons.length && !input) return null;

  const counter = text(one("question-index-counter"));
  if (counter && counter === state.lastCounter) return null;

  return {
    state: "question",
    counter,
    title: text(one("block-title")),
    choices: buttons.map((el) => ({
      selector: el.dataset.functionalSelector,
      text: text(el.querySelector('[data-functional-selector^="question-choice-text-"]')),
    })),
    multi: !!one("multi-select-submit-button"),
    typed: !!input,
    hasMedia: !!document.querySelector(
      '[data-functional-selector^="media-container"], [data-functional-selector="image-answer"]'
    ),
  };
}
"""

# One step of joining: resolves to "ok" when `done` holds, or to Kahoot's error text when the field
# named `rejected` is marked invalid (a wrong PIN shows up this way within a second).
JOIN_STEP_JS = """
() => {{
  const field = (name) => document.querySelector(`[data-functional-selector="${{name}}"]`);
  if ({done}) return "ok";
  const rejected = field("{rejected}");
  if (rejected && rejected.getAttribute("aria-invalid") === "true") {{
    const alert = [...document.querySelectorAll("[role=alert]")].map((el) => el.innerText.trim()).find(Boolean);
    return alert || "Kahoot bu girişi kabul etmedi.";
  }}
  return null;
}}
"""

QUESTION_GONE_JS = r"""
() => ![...document.querySelectorAll('[data-functional-selector^="answer-"]')]
        .some((el) => /^answer-\d+$/.test(el.dataset.functionalSelector))
      && !document.querySelector('[data-functional-selector="text-answer-input"]')
"""


@dataclass
class GameFeed:
    """What the server sends to every player's device, whether or not the page shows it.

    With "show questions on devices" off, the device still receives the complete first question in
    the game-start message; later questions come without text, but each one's picture id is
    announced one question ahead.
    """

    first_block: dict | None = None
    shuffled_questions: bool = False
    total_blocks: int | None = None
    image_ids: dict[int, str] = field(default_factory=dict)  # 0-based block index -> picture id
    time_limits: dict[int, float] = field(default_factory=dict)  # 0-based block index -> seconds to answer
    rank: int | None = None  # the bot's place and score, as of the last question result
    total_score: int | None = None


class KahootSession:
    def __init__(
        self,
        base_url: str,
        browser_channel: str,
        headless: bool,
        window: tuple[int, int, int, int] | None = None,
        on_game_start: Callable[[GameFeed], None] | None = None,
        on_game_ready: Callable[[GameFeed], None] | None = None,
    ) -> None:
        """`window` is (x, y, width, height) for the visible browser window; None keeps the default.

        `on_game_start` is called when the host starts the game and the first question becomes known
        (about ten seconds before it can be answered); `on_game_ready` once, about five seconds later,
        when the block count is known too.
        """
        self._base_url = base_url
        self._browser_channel = browser_channel
        self._headless = headless
        self._window = window
        self._on_game_start = on_game_start
        self._on_game_ready = on_game_ready
        self._game_ready_announced = False
        self._cookie_banner_rejected = False
        self.feed = GameFeed()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    def __enter__(self) -> KahootSession:
        self._playwright = sync_playwright().start()
        # "chromium" means Playwright's own build (needs `playwright install chromium`).
        channel = None if self._browser_channel == "chromium" else self._browser_channel
        args = list(BROWSER_ARGS)
        if self._window is not None:
            x, y, width, height = self._window
            args += [f"--window-position={x},{y}", f"--window-size={width},{height}"]
        self._browser = self._playwright.chromium.launch(channel=channel, headless=self._headless, args=args)
        if self._window is not None and not self._headless:
            # Let the page follow the requested window size instead of a fixed viewport.
            self._page = self._browser.new_page(no_viewport=True)
        else:
            self._page = self._browser.new_page(viewport={"width": 1100, "height": 800})
        self._page.on("websocket", lambda socket: socket.on("framereceived", self._on_frame))
        return self

    def __exit__(self, *exc_info: object) -> None:
        # The browser may already be gone (window closed, process killed); that must not mask the real exit reason.
        try:
            if self._browser is not None:
                self._browser.close()
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass

    def open_join_page(self) -> None:
        """Loads the PIN page, so that joining later only has to type the PIN and the nickname."""
        page = self._page
        for attempt in range(2):  # a short network hiccup should not cost the whole game
            try:
                page.goto(self._base_url, wait_until="domcontentloaded")
                break
            except PlaywrightError as error:
                logger.warning("join page did not load (attempt %d): %s", attempt + 1, error)
                if attempt:
                    raise RuntimeError(f"{self._base_url} açılamadı; internet bağlantısını kontrol et.") from None
        self._dismiss_cookie_banner()
        page.locator(sel(PIN_INPUT)).wait_for(timeout=JOIN_TIMEOUT_MS)

    def join(self, pin: str, nickname: str) -> None:
        """Expects the PIN page to be open (see open_join_page)."""
        page = self._page
        page.locator(sel(PIN_INPUT)).fill(pin, timeout=JOIN_TIMEOUT_MS)
        page.locator(sel(PIN_SUBMIT)).click()
        self._wait_for_join_step(
            JOIN_STEP_JS.format(done=f"!!field('{NAME_INPUT}')", rejected=PIN_INPUT),
            f"PIN {pin} kabul edilmedi ya da takma ad ekranı gelmedi "
            "(oyun başlamamış, kilitli veya rastgele isim modu açık olabilir).",
        )
        page.locator(sel(NAME_INPUT)).fill(nickname, timeout=JOIN_TIMEOUT_MS)
        page.locator(sel(NAME_SUBMIT)).click()
        self._wait_for_join_step(
            JOIN_STEP_JS.format(done=f"!field('{NAME_INPUT}')", rejected=NAME_INPUT),
            f"'{nickname}' takma adı kabul edilmedi (kullanımda ya da filtreye takıldı).",
        )
        if page.locator(sel(TWO_FACTOR_PAGE)).count():
            print("İki adımlı katılım açık: host ekranındaki şekil sırasını bot penceresinde elle gir.")

    def _wait_for_join_step(self, expression: str, timeout_message: str) -> None:
        """Raises RuntimeError with Kahoot's own message as soon as the page rejects the input."""
        try:
            outcome = self._page.wait_for_function(expression, timeout=JOIN_TIMEOUT_MS).json_value()
        except PlaywrightTimeoutError:
            raise RuntimeError(timeout_message) from None
        if outcome != "ok":
            raise RuntimeError(outcome)

    def wait_for_event(self, last_counter: str | None, awaiting_result: bool) -> dict:
        """Blocks until a new question, the awaited result, the game-over screen, or a closed window."""
        state = {"lastCounter": last_counter, "awaitingResult": awaiting_result}
        return self._wait(NEXT_EVENT_JS, state) or {"state": "closed"}

    def wait_until_question_gone(self) -> None:
        self._wait(QUESTION_GONE_JS, None)

    def screenshot_jpeg(self) -> bytes:
        return self._page.screenshot(type="jpeg", quality=85)

    def click_choices(self, selectors: list[str], multi: bool) -> None:
        for selector in selectors:
            self._page.locator(sel(selector)).click(timeout=CLICK_TIMEOUT_MS)
        if multi:
            self._page.locator(sel(MULTI_SUBMIT)).click(timeout=CLICK_TIMEOUT_MS)

    def type_answer(self, answer: str) -> None:
        field = self._page.locator(sel(TEXT_ANSWER_INPUT))
        field.fill(answer, timeout=CLICK_TIMEOUT_MS)
        field.press("Enter")

    def _wait(self, expression: str, arg: object) -> dict | None:
        """Returns None when the user closed the browser window while waiting."""
        try:
            handle = self._page.wait_for_function(expression, arg=arg, timeout=0, polling=50)
            return handle.json_value()
        except PlaywrightError:
            if self._page.is_closed() or not self._browser.is_connected():
                return None
            raise

    def _on_frame(self, payload: str | bytes) -> None:
        if not isinstance(payload, str):
            return
        try:
            messages = json.loads(payload)
        except ValueError:
            return
        for message in messages if isinstance(messages, list) else []:
            data = message.get("data") if isinstance(message, dict) else None
            if not isinstance(data, dict) or message.get("channel") != PLAYER_CHANNEL:
                continue
            try:
                content = json.loads(data.get("content") or "null")
            except (TypeError, ValueError):
                continue
            if isinstance(content, dict):
                logger.debug("server message id=%s: %s", data.get("id"), json.dumps(content, ensure_ascii=False)[:4000])
                self._absorb(data.get("id"), content)

    def _absorb(self, message_id: object, content: dict) -> None:
        feed = self.feed
        if message_id == MESSAGE_GAME_START:
            feed.first_block = content.get("firstGameBlockData")
            feed.shuffled_questions = bool((content.get("gameOptions") or {}).get("randomQuestions"))
            first_image_id = ((feed.first_block or {}).get("imageMetadata") or {}).get("id")
            if first_image_id:
                feed.image_ids[0] = first_image_id
            if self._on_game_start is not None:
                self._on_game_start(feed)
        elif message_id in (MESSAGE_QUESTION_RESULT, MESSAGE_GAME_OVER):
            if isinstance(content.get("rank"), int):
                feed.rank = content["rank"]
            if isinstance(content.get("totalScore"), int):
                feed.total_score = content["totalScore"]
        elif message_id in (MESSAGE_GET_READY, MESSAGE_QUESTION_START):
            index = content.get("gameBlockIndex")
            if isinstance(content.get("totalGameBlockCount"), int):
                feed.total_blocks = content["totalGameBlockCount"]
            next_image_id = ((content.get("nextGameBlockData") or {}).get("imageMetadata") or {}).get("id")
            if isinstance(index, int) and next_image_id:
                feed.image_ids[index + 1] = next_image_id
            if isinstance(index, int) and isinstance(content.get("timeAvailable"), (int, float)):
                feed.time_limits[index] = content["timeAvailable"] / 1000
            if self._on_game_ready is not None and not self._game_ready_announced:
                self._game_ready_announced = True
                self._on_game_ready(feed)

    def _dismiss_cookie_banner(self) -> None:
        # The consent banner can sit on top of the join button. Once rejected it stays away,
        # so later loads of the page do not wait for it again.
        if self._cookie_banner_rejected:
            return
        try:
            self._page.locator("#onetrust-reject-all-handler").click(timeout=3_000)
            self._cookie_banner_rejected = True
        except PlaywrightTimeoutError:
            pass
