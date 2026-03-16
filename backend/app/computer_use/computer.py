"""Computer environment interface for Computer Use workers."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Literal


@dataclass
class EnvState:
    """Represents the browser state returned after an action."""

    screenshot: bytes
    url: str


class Computer(abc.ABC):
    """Defines the browser action contract used by the worker loop."""

    @abc.abstractmethod
    def screen_size(self) -> tuple[int, int]:
        """Returns viewport width/height in pixels."""

    @abc.abstractmethod
    def open_web_browser(self) -> EnvState:
        """Opens a browser and returns current state."""

    @abc.abstractmethod
    def click_at(self, x: int, y: int) -> EnvState:
        """Clicks at x/y."""

    @abc.abstractmethod
    def hover_at(self, x: int, y: int) -> EnvState:
        """Moves cursor to x/y."""

    @abc.abstractmethod
    def type_text_at(
        self,
        x: int,
        y: int,
        text: str,
        press_enter: bool,
        clear_before_typing: bool,
    ) -> EnvState:
        """Types text at x/y and returns current state."""

    @abc.abstractmethod
    def scroll_document(
        self, direction: Literal["up", "down", "left", "right"]
    ) -> EnvState:
        """Scrolls the page."""

    @abc.abstractmethod
    def scroll_at(
        self,
        x: int,
        y: int,
        direction: Literal["up", "down", "left", "right"],
        magnitude: int,
    ) -> EnvState:
        """Scrolls at x/y."""

    @abc.abstractmethod
    def wait_5_seconds(self) -> EnvState:
        """Waits 5 seconds."""

    @abc.abstractmethod
    def go_back(self) -> EnvState:
        """Navigates back."""

    @abc.abstractmethod
    def go_forward(self) -> EnvState:
        """Navigates forward."""

    @abc.abstractmethod
    def search(self) -> EnvState:
        """Navigates to configured search homepage."""

    @abc.abstractmethod
    def navigate(self, url: str) -> EnvState:
        """Navigates directly to URL."""

    @abc.abstractmethod
    def key_combination(self, keys: list[str]) -> EnvState:
        """Presses key combinations."""

    @abc.abstractmethod
    def drag_and_drop(
        self, x: int, y: int, destination_x: int, destination_y: int
    ) -> EnvState:
        """Drag from x/y to destination_x/destination_y."""

    @abc.abstractmethod
    def current_state(self) -> EnvState:
        """Returns current screenshot/url state."""
