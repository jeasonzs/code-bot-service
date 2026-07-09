"""Reusable tile / footer view classes for dashboard pages.

These are not BasePage subclasses — they are draw helpers composed by a
BasePage (e.g. SystemPage) inside its `render()` method. They know how
to lay out a single region of the canvas and read the data values they
are given, but they don't know anything about collectors, time, or
which page they belong to.

Why this exists: SystemPage's 2x2 dashboard had 4 nearly-identical
tile renderers (CPU / MEM / TEMP / FREQ) and 1 horizontal-strip footer.
Extracting the shared geometry (icon left, title below, big value
right, optional dotted bar) into a single TileView class removes the
copy-paste; concrete views (cpu_view.py, etc.) just provide their
own data, color and icon, then call TileView.draw(canvas).
"""
