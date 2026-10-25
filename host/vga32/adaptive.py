"""Adaptive palette: while a window whose app sets adaptive_palette = True is focused, the screen's
16 colors are re-chosen from the board's 64 to suit what that app is rendering (for example every
shade a dark Quake level needs), and the normal palette comes back when focus moves on.

The app provides a small full-color sample of its frame (GameApp does it in g.dither()). Every
CHECK seconds a candidate palette is picked from it; it is only adopted when it fits clearly
better than the current one, because a switch makes the whole screen resend once.
"""
import threading
import time

from . import palette

CHECK = 0.7          # seconds between palette checks
GAIN = 0.8           # switch only when the new palette's error is below this fraction of the current one


class PaletteManager:
    def __init__(self, desk):
        self.desk = desk
        self.owner = None            # window id the adaptive palette is for
        self.current = None          # its color list, or None for the normal palette
        self.building = False
        self.last = 0.0
        self.switches = 0

    def wanted_owner(self):
        d = self.desk
        if not d.settings.get("adaptive_palette", True) or not d.windows:
            return None
        win = d.windows[-1]
        if win.minimized or win.shaded or not getattr(win.app, "adaptive_palette", False):
            return None
        return win

    def update(self):
        """Called from the desktop loop."""
        win = self.wanted_owner()
        if win is None:
            if self.current is not None:
                self._apply(None, None)
            return
        now = time.monotonic()
        if self.building or now - self.last < CHECK:
            return
        sample = win.app.palette_sample
        if sample is None:
            return
        self.last = now
        cand = palette.choose_palette(sample)
        if self.current is not None and win.id == self.owner:
            if cand == self.current or palette.palette_error(sample, cand) >= GAIN * palette.palette_error(
                    sample, self.current):
                return
        self.building = True
        wid = win.id

        def build():
            try:
                state = palette.PaletteState(cand, plan=True)       # ~0.3 s: off the desktop thread
            except Exception:
                state = None
            self.desk.post(lambda d: self._apply(state, cand, wid))
        threading.Thread(target=build, daemon=True).start()

    def _apply(self, state, colors, wid=None):
        d = self.desk
        self.building = False
        if colors is not None and (not d.windows or d.windows[-1].id != wid or state is None):
            return                   # focus moved while building
        palette.activate(state)
        palette.DISPLAY[:] = colors if colors is not None else d.theme.palette
        self.current, self.owner = colors, wid
        d.dev.palette(palette.DISPLAY)
        d.screen.invalidate()        # every index may mean a new color now: send the whole screen
        d.bg = None                  # the background image was dithered for the old palette
        d.dirty = True
        self.switches += 1
        d.bus.publish("palette.changed", adaptive=colors is not None, window_id=wid, colors=colors)

    def reset(self):
        """After a theme change: drop back to the theme palette; the next update re-adapts."""
        self.current = self.owner = None
        palette.activate(None)
