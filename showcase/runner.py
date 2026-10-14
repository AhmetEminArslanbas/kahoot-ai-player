"""Runs the bot process for the launcher window: prepares it ahead of time, tells it the PIN later,
and collects its output and progress events."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from config import EVENT_PREFIX, MODES

PROJECT_DIR = Path(__file__).parent
SETTINGS_PATH = PROJECT_DIR / "live_settings.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Demo mode: the bot plays the offline mock game in mock/ instead of kahoot.it.
DEMO = "--demo" in sys.argv or os.environ.get("KAHOOT_DEMO") == "1"


def save_live_settings(mode: str, max_seconds: float, confidence: int) -> bool:
    """Writes the settings the running bot re-reads before each question; False when they are invalid."""
    if mode not in MODES or max_seconds <= 0 or not 0 <= confidence <= 100:
        return False
    settings = {"mode": mode, "max_seconds": max_seconds, "confidence": confidence}
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings), encoding="utf-8")
    os.replace(temporary, SETTINGS_PATH)  # atomic, so the bot never reads a half-written file
    return True


class BotRunner:
    """One bot process at a time; its console lines and its progress events are kept for display."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._lines: list[str] = []
        self._events: list[dict] = []
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def prepare(self, headless: bool = False) -> None:
        """Starts a bot that opens the browser and warms Claude up, then waits to be told the PIN.

        It reports a "prepared" event when it is ready; `join` can be called from then on.
        """
        if self.running:
            raise RuntimeError("Bot zaten çalışıyor.")
        command = [
            # python.exe even when the launcher itself runs under the windowless pythonw.exe
            str(Path(sys.executable).with_name("python.exe")), "-u", "main.py",
            "--settings", str(SETTINGS_PATH),
            "--events",
        ]
        if headless:
            command.append("--headless")
        if DEMO:
            command += ["--base-url", (PROJECT_DIR / "mock" / "mock_kahoot.html").as_uri()]
        with self._lock:
            self._lines.clear()
            self._events.clear()
        self._process = subprocess.Popen(
            command, cwd=PROJECT_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
        )
        threading.Thread(target=self._read_output, args=(self._process,), daemon=True).start()

    def join(self, game: str, name: str) -> bool:
        """Tells the prepared bot which game to join; False when there is no bot to tell."""
        if not self.running:
            return False
        try:
            self._process.stdin.write(json.dumps({"game": game, "name": name}) + "\n")
            self._process.stdin.flush()
        except OSError:
            return False
        return True

    def stop(self) -> bool:
        """True when a running bot was stopped."""
        if not self.running:
            return False
        # /T also ends the browser and the claude processes the bot started
        subprocess.run(["taskkill", "/PID", str(self._process.pid), "/T", "/F"],
                       capture_output=True, creationflags=NO_WINDOW)
        return True

    def lines_since(self, index: int) -> list[str]:
        """Console lines from position `index` on; pass the count already shown to get only new ones."""
        with self._lock:
            return self._lines[index:]

    def events_since(self, index: int) -> list[dict]:
        with self._lock:
            return self._events[index:]

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        for line in process.stdout:
            event = None
            if line.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(line[len(EVENT_PREFIX):])
                except ValueError:
                    pass  # not an event after all: show it as an ordinary line
            with self._lock:
                if event is not None:
                    self._events.append(event)
                else:
                    self._lines.append(line)
