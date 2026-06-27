"""Base class for all page renderers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..canvas import Canvas
from ..widgets import draw_indicator, draw_title, draw_hint


class BasePage(ABC):
    """A page renderer for one of the 7 screen types.

    Subclasses override render() to draw content into the canvas.
    Optional on_touch() to handle touch events from device.
    """

    #: Page title shown at top
    title: str = "Page"

    #: Whether this page is enabled (from YAML config)
    enabled: bool = True

    @abstractmethod
    def render(self, canvas: Canvas) -> None:
        """Render the page content into the canvas.

        Subclasses should:
        1. Fill background (canvas.fill)
        2. Draw content
        3. Call draw_indicator / draw_title / draw_hint as needed
        """
        ...

    def on_touch(self, event_type: int, x: int, y: int) -> Optional[str]:
        """Handle a touch event.

        Returns:
            None - no action
            str - "action_id" to trigger (e.g. "keystroke:git status")
        """
        return None
