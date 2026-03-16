"""Local Playwright-backed computer implementation for Computer Use."""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Literal

from backend.app.computer_use.computer import Computer, EnvState
from backend.app.computer_use.errors import ComputerUseDependencyError


PLAYWRIGHT_KEY_MAP = {
    "backspace": "Backspace",
    "tab": "Tab",
    "return": "Enter",
    "enter": "Enter",
    "shift": "Shift",
    "control": "ControlOrMeta",
    "alt": "Alt",
    "escape": "Escape",
    "space": "Space",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "end": "End",
    "home": "Home",
    "left": "ArrowLeft",
    "up": "ArrowUp",
    "right": "ArrowRight",
    "down": "ArrowDown",
    "insert": "Insert",
    "delete": "Delete",
    "f1": "F1",
    "f2": "F2",
    "f3": "F3",
    "f4": "F4",
    "f5": "F5",
    "f6": "F6",
    "f7": "F7",
    "f8": "F8",
    "f9": "F9",
    "f10": "F10",
    "f11": "F11",
    "f12": "F12",
    "command": "Meta",
}


def _env_true(var_name: str) -> bool:
    value = os.getenv(var_name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


class PlaywrightComputer(Computer):
    """Runs browser actions in a local Playwright Chromium session."""

    def __init__(
        self,
        *,
        screen_size: tuple[int, int] = (1440, 900),
        initial_url: str = "https://www.google.com",
        search_engine_url: str = "https://www.google.com",
    ) -> None:
        self._screen_size = screen_size
        self._initial_url = initial_url
        self._search_engine_url = search_engine_url
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self.debug_url: str | None = None

    def __enter__(self) -> "PlaywrightComputer":
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ModuleNotFoundError as exc:
            raise ComputerUseDependencyError(
                "Missing Playwright Python package. Install with: "
                "'.venv/bin/pip install playwright' and run "
                "'.venv/bin/playwright install chromium'."
            ) from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            args=[
                "--disable-extensions",
                "--disable-file-system",
                "--disable-plugins",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
            ],
            headless=_env_true("PLAYWRIGHT_HEADLESS"),
        )
        self._context = self._browser.new_context(
            viewport={
                "width": self._screen_size[0],
                "height": self._screen_size[1],
            }
        )
        self._page = self._context.new_page()
        self._page.goto(self._initial_url)
        self._context.on("page", self._handle_new_page)
        self.debug_url = self._page.url
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass

    def _handle_new_page(self, new_page: Any) -> None:
        new_url = new_page.url
        try:
            new_page.close()
        except Exception:
            pass
        self._page.goto(new_url)

    def screen_size(self) -> tuple[int, int]:
        viewport_size = self._page.viewport_size
        if viewport_size:
            return viewport_size["width"], viewport_size["height"]
        return self._screen_size

    def open_web_browser(self) -> EnvState:
        return self.current_state()

    def click_at(self, x: int, y: int) -> EnvState:
        self._page.mouse.click(x, y)
        self._page.wait_for_load_state()
        return self.current_state()

    def hover_at(self, x: int, y: int) -> EnvState:
        self._page.mouse.move(x, y)
        self._page.wait_for_load_state()
        return self.current_state()

    def type_text_at(
        self,
        x: int,
        y: int,
        text: str,
        press_enter: bool,
        clear_before_typing: bool,
    ) -> EnvState:
        self._page.mouse.click(x, y)
        self._page.wait_for_load_state()

        if clear_before_typing:
            if sys.platform == "darwin":
                self.key_combination(["Command", "A"])
            else:
                self.key_combination(["Control", "A"])
            self.key_combination(["Delete"])

        self._page.keyboard.type(text)
        self._page.wait_for_load_state()

        if press_enter:
            self.key_combination(["Enter"])

        self._page.wait_for_load_state()
        return self.current_state()

    def _horizontal_document_scroll(
        self, direction: Literal["left", "right"]
    ) -> EnvState:
        horizontal_scroll_amount = self.screen_size()[0] // 2
        sign = "-" if direction == "left" else ""
        self._page.evaluate(f"window.scrollBy({sign}{horizontal_scroll_amount}, 0); ")
        self._page.wait_for_load_state()
        return self.current_state()

    def scroll_document(
        self, direction: Literal["up", "down", "left", "right"]
    ) -> EnvState:
        if direction == "down":
            return self.key_combination(["PageDown"])
        if direction == "up":
            return self.key_combination(["PageUp"])
        if direction in ("left", "right"):
            return self._horizontal_document_scroll(direction)
        raise ValueError(f"Unsupported direction: {direction}")

    def scroll_at(
        self,
        x: int,
        y: int,
        direction: Literal["up", "down", "left", "right"],
        magnitude: int,
    ) -> EnvState:
        self._page.mouse.move(x, y)
        self._page.wait_for_load_state()

        dx = 0
        dy = 0
        if direction == "up":
            dy = -magnitude
        elif direction == "down":
            dy = magnitude
        elif direction == "left":
            dx = -magnitude
        elif direction == "right":
            dx = magnitude
        else:
            raise ValueError(f"Unsupported direction: {direction}")

        self._page.mouse.wheel(dx, dy)
        self._page.wait_for_load_state()
        return self.current_state()

    def wait_5_seconds(self) -> EnvState:
        time.sleep(5)
        return self.current_state()

    def go_back(self) -> EnvState:
        self._page.go_back()
        self._page.wait_for_load_state()
        return self.current_state()

    def go_forward(self) -> EnvState:
        self._page.go_forward()
        self._page.wait_for_load_state()
        return self.current_state()

    def search(self) -> EnvState:
        return self.navigate(self._search_engine_url)

    def navigate(self, url: str) -> EnvState:
        normalized = url.strip()
        if not normalized.startswith(("http://", "https://")):
            normalized = "https://" + normalized
        self._page.goto(normalized)
        self._page.wait_for_load_state()
        self.debug_url = normalized
        return self.current_state()

    def key_combination(self, keys: list[str]) -> EnvState:
        normalized_keys = [PLAYWRIGHT_KEY_MAP.get(k.lower(), k) for k in keys]
        for key in normalized_keys[:-1]:
            self._page.keyboard.down(key)
        self._page.keyboard.press(normalized_keys[-1])
        for key in reversed(normalized_keys[:-1]):
            self._page.keyboard.up(key)
        return self.current_state()

    def drag_and_drop(
        self, x: int, y: int, destination_x: int, destination_y: int
    ) -> EnvState:
        self._page.mouse.move(x, y)
        self._page.wait_for_load_state()
        self._page.mouse.down()
        self._page.wait_for_load_state()
        self._page.mouse.move(destination_x, destination_y)
        self._page.wait_for_load_state()
        self._page.mouse.up()
        return self.current_state()

    def current_state(self) -> EnvState:
        self._page.wait_for_load_state()
        time.sleep(0.5)
        screenshot_bytes = self._page.screenshot(type="png", full_page=False)
        return EnvState(screenshot=screenshot_bytes, url=self._page.url)
