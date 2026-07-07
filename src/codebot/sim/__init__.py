"""Simulation mode: render LCD output to a local web page, no real USB device.

Activated by `codebotd start --sim`. Starts an HTTP server on localhost
(default 8080) serving:
  GET  /           → static index.html (canvas + mouse handlers)
  GET  /frame.png  → current Pillow Image as PNG (polled every ~33ms by browser)
  POST /touch      → JSON {event, x, y} translated into daemon _handle_touch()
"""

from .server import SimServer

__all__ = ["SimServer"]