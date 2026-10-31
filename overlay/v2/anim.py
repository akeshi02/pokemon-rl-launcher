"""anim.py -- the launcher's motion engine (v2 / M0).

Everything animated in v2 goes through here, for one reason: a training tool may
never sell animation at the cost of emulator throughput.  So the engine has two
global switches -- *reduce motion* (accessibility) and *performance mode* (set
while a run is active) -- and both collapse every duration to zero.  The state
change always happens; only the presentation is skipped.

Nothing in M0 animates unless a screen explicitly asks it to, so wiring this in
does not change the 1.0.0 look or cost.
"""

from __future__ import annotations

import math
import time

import theme as T

__all__ = [
    "EASINGS", "lerp", "lerp_color", "parse_color",
    "Motion", "MOTION", "MOTION_SPEC", "spec", "set_performance",
    "set_reduce_motion", "reduce_motion_from_os",
    "Animator", "TweenNumber", "Toasts", "Toast", "Sequence", "breathe",
    "Progress",
]

# ---------------------------------------------------------------------------
# Easing
# ---------------------------------------------------------------------------

def linear(t: float) -> float:
    return t


def ease_out_quad(t: float) -> float:
    return 1.0 - (1.0 - t) ** 2


def ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def ease_in_cubic(t: float) -> float:
    return t ** 3


def ease_in_out_quad(t: float) -> float:
    return 2 * t * t if t < 0.5 else 1.0 - (-2 * t + 2) ** 2 / 2.0


def ease_in_out_sine(t: float) -> float:
    """Half-cosine ease -- the 'sine' row of the motion spec (§3)."""
    return -(math.cos(math.pi * t) - 1.0) / 2.0


def ease_out_back(t: float) -> float:
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def spring(t: float) -> float:
    """A cheap critically-damped-ish overshoot (no state, no physics loop)."""
    if t >= 1.0:
        return 1.0
    return 1.0 - math.pow(2.0, -10.0 * t) * math.cos(t * math.pi * 3.0)


EASINGS = {
    "linear": linear,
    "easeOutQuad": ease_out_quad,
    "easeOutCubic": ease_out_cubic,
    "easeInCubic": ease_in_cubic,
    "easeInOutQuad": ease_in_out_quad,
    "sine": ease_in_out_sine,
    "easeOutBack": ease_out_back,
    "spring": spring,
}


# The motion spec from the roadmap (§3) lives here so no duration or easing is
# ever invented at a call site -- a new animation needs a row in this table.
# Durations are milliseconds.
MOTION_SPEC = {
    "page":         {"ms": 220, "easing": "easeOutCubic"},    # select -> grid
    "page_slide":   12,                                       # px of the above
    "dialog_open":  {"ms": 140, "easing": "easeOutQuad"},
    "dialog_close": {"ms": 110, "easing": "easeOutQuad"},
    "toast_in":     {"ms": 180, "easing": "easeOutCubic"},
    "toast_out":    {"ms": 140, "easing": "easeOutCubic"},
    "button":       {"ms": 90,  "easing": "linear"},
    "number":       {"ms": 300, "easing": "easeOutCubic"},
    "sparkline":    {"ms": 120, "easing": "easeInOutQuad"},
    "component":    {"ms": 200, "easing": "easeOutCubic"},
    "tile_border":  {"ms": 90,  "easing": "linear"},
    "running_dot":  {"ms": 1400, "easing": "sine"},
    "retile":       {"ms": 120, "easing": "linear"},
    "progress":     {"ms": 900, "easing": "linear"},          # one 0->1 sweep
}


def spec(name: str) -> dict:
    """Look up a row of :data:`MOTION_SPEC` (raises ``KeyError`` if unknown)."""
    return MOTION_SPEC[name]


def lerp(a, b, t: float):
    return a + (b - a) * t


# ---------------------------------------------------------------------------
# Colour interpolation (for CTk props: fg_color / text_color / border_color)
# ---------------------------------------------------------------------------

_NAMED = {"white": (255, 255, 255), "black": (0, 0, 0), "gray": (190, 190, 190)}


def parse_color(value):
    """Return (r, g, b) for a tk/CTk colour, or None if we can't interpolate it."""
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            return None
        value = value[1] if T.get_appearance() == "dark" else value[0]
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if s in ("transparent", ""):
        return None
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if len(h) != 6:
            return None
        try:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return None
    if s in _NAMED:
        return _NAMED[s]
    if s.startswith("gray") and s[4:].isdigit():  # customtkinter's gray0..gray100
        v = round(max(0, min(100, int(s[4:]))) * 255 / 100)
        return (v, v, v)
    return None


def lerp_color(a, b, t: float):
    """Interpolate two colours; returns a hex string, or ``b`` if not possible."""
    ca, cb = parse_color(a), parse_color(b)
    if ca is None or cb is None:
        return b
    return "#{:02x}{:02x}{:02x}".format(
        int(round(lerp(ca[0], cb[0], t))),
        int(round(lerp(ca[1], cb[1], t))),
        int(round(lerp(ca[2], cb[2], t))),
    )


# ---------------------------------------------------------------------------
# Motion policy
# ---------------------------------------------------------------------------

class Motion:
    """Global animation policy shared by every Animator/Toast in the app."""

    def __init__(self, fps: int = 30):
        self.fps = fps
        self.reduce_motion = False   # accessibility switch
        self.performance = False     # True while a training run is active

    @property
    def enabled(self) -> bool:
        return not (self.reduce_motion or self.performance)

    def duration(self, ms) -> int:
        """Clamp a requested duration to the current policy (0 == instant)."""
        if not self.enabled:
            return 0
        try:
            return max(0, int(ms))
        except (TypeError, ValueError):
            return 0

    def frame_ms(self, fps: int | None = None) -> int:
        hz = fps or self.fps
        return max(8, int(round(1000.0 / max(1, hz))))


MOTION = Motion()
_TIMER = {"raised": False, "active": 0}


def _timer_resolution(on: bool) -> None:
    """Raise the OS timer resolution while animating, release it when idle.

    Windows' default ~15.6 ms timer granularity is what caps Tk's ``after`` at
    ~22 fps no matter what interval you ask for.  timeBeginPeriod(1) makes
    ``after(33)`` actually mean 33 ms -- and it is released again the moment no
    tween is running, so an idle or training-time UI costs nothing extra.
    """
    import sys
    try:
        if on and not _TIMER["raised"]:
            if sys.platform.startswith("win"):
                import ctypes
                ctypes.windll.winmm.timeBeginPeriod(1)
            _TIMER["raised"] = True
        elif not on and _TIMER["raised"]:
            if sys.platform.startswith("win"):
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            _TIMER["raised"] = False
    except Exception:
        pass


def _acquire_timer() -> None:
    _TIMER["active"] += 1
    if _TIMER["active"] == 1:
        _timer_resolution(True)


def _release_timer() -> None:
    if _TIMER["active"] > 0:
        _TIMER["active"] -= 1
        if _TIMER["active"] == 0:
            _timer_resolution(False)


def set_performance(on: bool) -> None:
    """Turn animation off (on=True) while training runs, on again (False) after."""
    MOTION.performance = bool(on)
    if not MOTION.enabled:
        _TIMER["active"] = 0
        _timer_resolution(False)


def set_reduce_motion(on: bool) -> None:
    MOTION.reduce_motion = bool(on)
    if not MOTION.enabled:
        _TIMER["active"] = 0
        _timer_resolution(False)


def reduce_motion_from_os() -> bool:
    """Best-effort read of the OS 'reduce motion' style setting."""
    import sys
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes
        SPI_GETCLIENTAREAANIMATION = 0x1042
        enabled = wintypes.BOOL()
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0)
        return bool(ok) and not bool(enabled.value)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Animator
# ---------------------------------------------------------------------------

class Animator:
    """Drives tweens on one widget via ``widget.after`` (main thread only)."""

    def __init__(self, widget, motion: Motion | None = None, fps: int | None = None):
        self.widget = widget
        self.motion = motion or MOTION
        self.fps = fps or self.motion.fps
        self._jobs: dict[object, str] = {}
        self._timed: set = set()  # tags that hold a timer-resolution lease

    # -- scheduling helpers -------------------------------------------------
    def _dead(self) -> bool:
        try:
            return not bool(self.widget.winfo_exists())
        except Exception:
            return True

    def _hold(self, tag) -> None:
        if tag not in self._timed:
            self._timed.add(tag)
            _acquire_timer()

    def _drop(self, tag) -> None:
        if tag in self._timed:
            self._timed.discard(tag)
            _release_timer()

    def _schedule(self, delay_ms: int, fn, tag):
        try:
            job = self.widget.after(delay_ms, fn)
        except Exception:
            return None
        self._jobs[tag] = job
        return job

    def stop(self, tag) -> None:
        job = self._jobs.pop(tag, None)
        self._drop(tag)
        if job is None:
            return
        try:
            self.widget.after_cancel(job)
        except Exception:
            pass

    def stop_all(self) -> None:
        for tag in list(self._jobs):
            self.stop(tag)
        for tag in list(self._timed):
            self._drop(tag)

    # -- tweening -----------------------------------------------------------
    def tween(self, apply_fn, start, end, duration=200,
              easing: str = "easeOutCubic", on_done=None, tag=None, fps=None):
        """Call ``apply_fn(value)`` from ``start`` to ``end`` over ``duration`` ms.

        Returns the tag used (pass it to :meth:`stop`).  ``apply_fn`` is also
        called once with ``end`` immediately when motion is disabled.
        """
        ease = EASINGS.get(easing, ease_out_cubic)
        ms = self.motion.duration(duration)
        tag = tag if tag is not None else f"tween:{id(apply_fn)}"
        self.stop(tag)

        if ms <= 0 or self._dead():
            self._safe(apply_fn, end)
            if on_done:
                self._safe(on_done, None)
            return tag

        self._hold(tag)
        frame = self.motion.frame_ms(fps or self.fps)
        t0 = time.perf_counter()

        def _step():
            if self._dead():
                self._jobs.pop(tag, None)
                self._drop(tag)
                return
            p = min(1.0, (time.perf_counter() - t0) * 1000.0 / ms)
            self._safe(apply_fn, lerp(start, end, ease(p)))
            if p >= 1.0:
                self._jobs.pop(tag, None)
                self._safe(apply_fn, end)
                self._drop(tag)
                if on_done:
                    self._safe(on_done, None)
                return
            job = self._schedule(frame, _step, tag)
            if job is None:
                self._safe(apply_fn, end)
                self._drop(tag)

        _step()
        return tag

    @staticmethod
    def _safe(fn, value) -> None:
        try:
            if value is None:
                fn()
            else:
                fn(value)
        except Exception:
            pass  # a destroyed widget must never crash a tween

    # -- property tweening --------------------------------------------------
    def tween_props(self, to: dict, duration=200, easing="easeOutCubic",
                    to_from: dict | None = None, on_done=None, tag=None, fps=None):
        """Tween CTk widget properties (numeric or colour) to ``to``.

        Unknown / non-interpolatable properties are applied at the end only.
        """
        starts = {}
        for key in to:
            if to_from and key in to_from:
                starts[key] = to_from[key]
                continue
            try:
                starts[key] = self.widget.cget(key)
            except Exception:
                starts[key] = None

        def apply(t):
            kwargs = {}
            for key, target in to.items():
                start = starts.get(key)
                if isinstance(target, (int, float)) and isinstance(start, (int, float)):
                    kwargs[key] = type(target)(round(lerp(start, target, t))) \
                        if isinstance(target, int) else lerp(start, target, t)
                elif parse_color(start) and parse_color(target):
                    kwargs[key] = lerp_color(start, target, t)
                elif t >= 1.0:
                    kwargs[key] = target
            if kwargs:
                try:
                    self.widget.configure(**kwargs)
                except Exception:
                    pass

        return self.tween(apply, 0.0, 1.0, duration, easing, on_done, tag, fps)


# ---------------------------------------------------------------------------
# Numbers (timer, KPI counters)
# ---------------------------------------------------------------------------

class TweenNumber:
    """Counts a displayed number up/down instead of snapping."""

    def __init__(self, widget, duration=MOTION_SPEC["number"]["ms"], decimals=0, motion=None,
                 easing=MOTION_SPEC["number"]["easing"], fps=None, prefix="", suffix="",
                 thousands=True):
        self.widget = widget
        self.duration = duration
        self.decimals = decimals
        self.prefix = prefix
        self.suffix = suffix
        self.thousands = thousands
        self.value = 0.0
        self._animator = Animator(widget, motion, fps)
        self._easing = easing
        self._tag = f"tweennum:{id(self)}"
        self._rendered = None

    def render(self, value: float) -> str:
        if self.decimals <= 0:
            body = f"{int(round(value)):,}" if self.thousands else str(int(round(value)))
        else:
            body = f"{value:,.{self.decimals}f}" if self.thousands else f"{value:.{self.decimals}f}"
        return f"{self.prefix}{body}{self.suffix}"

    def set(self, value: float, snap: bool = False) -> None:
        target = float(value)
        if snap or not self._animator.motion.enabled:
            self.value = target
            self._apply(target)
            return
        start = self.value
        self.value = target

        def apply(v):
            self._apply(v)

        self._animator.tween(apply, start, target, self.duration,
                             self._easing, tag=self._tag)

    def _apply(self, value: float) -> None:
        text = self.render(value)
        if text == self._rendered:
            return
        self._rendered = text
        try:
            self.widget.configure(text=text)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Toasts
# ---------------------------------------------------------------------------

class Toasts:
    """A small stack of transient messages in the parent's bottom-right corner."""

    WIDTH = 320
    MARGIN = 16
    GAP = 8

    def __init__(self, parent, motion: Motion | None = None, fps: int | None = None):
        self.parent = parent
        self.motion = motion or MOTION
        self.fps = fps
        self._items: list[dict] = []

    # -- public -------------------------------------------------------------
    def show(self, text: str, kind: str = "info", timeout: int = 4000,
             title: str | None = None) -> None:
        import customtkinter as ctk

        frame = ctk.CTkFrame(self.parent, corner_radius=T.RADIUS["lg"],
                             border_width=1, border_color=T.c("border"),
                             fg_color=T.c(f"toast_{kind}" if kind != "info" else "toast_info"),
                             width=self.WIDTH)
        accent = {"ok": T.col("ok"), "warn": T.col("warn"),
                  "error": T.col("danger")}.get(kind, T.col("accent"))
        bar = ctk.CTkFrame(frame, width=4, corner_radius=2, fg_color=accent)
        bar.pack(side="left", fill="y", padx=(8, 0), pady=10)
        body = ctk.CTkFrame(frame, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        if title:
            ctk.CTkLabel(body, text=title, font=T.f("small", "bold"),
                         anchor="w", justify="left").pack(anchor="w")
        ctk.CTkLabel(body, text=text, font=T.f("small"), anchor="w", justify="left",
                     wraplength=self.WIDTH - 60).pack(anchor="w")

        item = {"frame": frame, "animator": Animator(frame, self.motion, self.fps), "shown": False}
        # Marks the widget so ``LauncherApp._clear_root_children`` leaves toasts
        # alone when it wipes a screen (they belong to the window, not a screen).
        try:
            frame._poke_toast = True
        except Exception:
            pass
        self._items.append(item)
        self._relayout()

        if self.motion.enabled:
            item["animator"].tween(lambda x: self._place(item, x), self.WIDTH, -self.MARGIN,
                                   duration=spec("toast_in")["ms"], easing=spec("toast_in")["easing"],
                                   tag=f"toast-in:{id(item)}",
                                   on_done=lambda: item.update(shown=True))
        else:
            self._place(item, -self.MARGIN)
            item["shown"] = True

        if timeout:
            try:
                self.parent.after(timeout, lambda: self.dismiss(item))
            except Exception:
                pass

    def dismiss(self, item) -> None:
        if item not in self._items:
            return
        frame = item["frame"]
        if not self.motion.enabled:
            self._remove(item)
            return

        def done():
            self._remove(item)

        item["animator"].tween(lambda x: self._place(item, x), -self.MARGIN, self.WIDTH,
                               duration=spec("toast_out")["ms"], easing=spec("toast_out")["easing"],
                               tag=f"toast-out:{id(item)}", on_done=done)

    def reflow(self) -> None:
        """Re-place every toast so it sits above a freshly built screen."""
        for item in self._items:
            self._place(item, item.get("x", -self.MARGIN))
        self._relayout()

    def clear(self) -> None:
        for item in list(self._items):
            self._remove(item)

    # -- internals ----------------------------------------------------------
    def _place(self, item, x) -> None:
        item["x"] = x
        try:
            item["frame"].place(relx=1.0, rely=1.0, x=x, y=item.get("y", -self.MARGIN),
                                anchor="se")
        except Exception:
            pass

    def _relayout(self) -> None:
        """Stack items upwards from the bottom-right corner, keeping each x."""
        y = -self.MARGIN
        for item in reversed(self._items):
            item["y"] = y
            self._place(item, item.get("x", -self.MARGIN))
            try:
                self.parent.update_idletasks()
                h = item["frame"].winfo_height() or 54
            except Exception:
                h = 54
            y -= h + self.GAP

    def _remove(self, item) -> None:
        if item in self._items:
            self._items.remove(item)
        try:
            item["animator"].stop_all()
        except Exception:
            pass
        try:
            item["frame"].destroy()
        except Exception:
            pass
        self._relayout()


Toast = Toasts


# ---------------------------------------------------------------------------
# Sequencing + looping helpers
# ---------------------------------------------------------------------------

class Sequence:
    """Runs zero-argument steps back-to-back on the main thread.

    Each step may return a duration in ms (the next step waits that long) or
    ``None`` to continue immediately.  Used for page transitions.
    """

    def __init__(self, widget, motion: Motion | None = None):
        self.widget = widget
        self.motion = motion or MOTION
        self._tag = f"seq:{id(self)}"
        self._job = None

    def play(self, steps, on_done=None) -> None:
        self.cancel()
        queue = list(steps)

        def _next():
            self._job = None
            if not queue:
                if on_done:
                    try:
                        on_done()
                    except Exception:
                        pass
                return
            step = queue.pop(0)
            try:
                wait = step()
            except Exception:
                wait = None
            delay = self.motion.duration(wait) if wait else 0
            try:
                self._job = self.widget.after(delay, _next)
            except Exception:
                pass

        _next()

    def cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None


def breathe(widget, attr: str = "text_color", period: int | None = None,
            color_from=None, color_to=None, motion: Motion | None = None, fps: int = 20,
            always: bool = False):
    """Loop a subtle colour pulse (e.g. the 'running' dot).  Returns a stopper.

    With motion disabled this normally applies ``color_to`` once and schedules
    nothing -- performance mode costs exactly zero frames.  ``always=True`` is
    for the one animation the roadmap allows *during* a run: the "running"
    indicator, a single cheap pulse.  Reduce-motion still wins over ``always``.
    """
    motion = motion or MOTION
    if period is None:
        period = spec("running_dot")["ms"]
    color_from = color_from or T.col("text_faint")
    color_to = color_to or T.col("accent")
    animator = Animator(widget, motion, fps)
    # "always" pulses even in performance mode (a run is active); reduce-motion
    # is the accessibility veto and always stops it.
    pulse_on = motion.enabled or (always and not motion.reduce_motion)
    alive = {"on": True}
    held = {"on": False}
    t0 = time.perf_counter()

    def _finish():
        if held["on"]:
            held["on"] = False
            _release_timer()

    def _pulse():
        if not alive["on"]:
            _finish()
            return
        phase = (time.perf_counter() - t0) / max(0.001, period / 1000.0)
        t = 0.5 - 0.5 * math.cos(phase * 2 * math.pi)
        try:
            widget.configure(**{attr: lerp_color(color_from, color_to, t)})
        except Exception:
            _finish()
            return
        if pulse_on:
            try:
                widget.after(motion.frame_ms(fps), _pulse)
            except Exception:
                _finish()

    if pulse_on:
        _acquire_timer()
        held["on"] = True
        _pulse()
    else:
        try:
            widget.configure(**{attr: color_to})
        except Exception:
            pass

    def stop():
        alive["on"] = False
        _finish()
        animator.stop_all()

    return stop


class Progress:
    """A single, policy-aware indeterminate progress bar.

    For "busy" states such as scanning environments or checking packages.  It is
    exactly one widget and it parks itself the moment motion is off, so it can
    never cost frames while a training run is active.
    """

    def __init__(self, parent, motion: Motion | None = None, period: int | None = None,
                 idle: float = 0.35, width: int = 200, height: int = 6, fps: int = 30,
                 **kwargs):
        import customtkinter as ctk
        self.motion = motion or MOTION
        self.period = max(120, int(period or spec("progress")["ms"]))
        self.idle = float(idle)
        self._fps = fps
        self._alive = True
        self._held = False
        self.bar = ctk.CTkProgressBar(
            parent, width=width, height=height,
            corner_radius=max(2, height // 2),
            progress_color=T.c("accent"), fg_color=T.c("card"), **kwargs)
        self.bar.set(self.idle)
        if self.motion.enabled:
            self._hold()
            self._t0 = time.perf_counter()
            self._tick()

    # -- internals ----------------------------------------------------------
    def _hold(self) -> None:
        if not self._held:
            self._held = True
            _acquire_timer()

    def _release(self) -> None:
        if self._held:
            self._held = False
            _release_timer()

    def _tick(self) -> None:
        if not self._alive or not self.motion.enabled:
            self._release()
            return
        phase = ((time.perf_counter() - self._t0) / (self.period / 1000.0)) % 1.0
        try:
            self.bar.set(phase)
        except Exception:
            self._release()
            return
        try:
            self.bar.after(self.motion.frame_ms(self._fps), self._tick)
        except Exception:
            self._release()

    # -- layout / lifecycle -------------------------------------------------
    def pack(self, **kwargs):
        self.bar.pack(**kwargs)
        return self

    def grid(self, **kwargs):
        self.bar.grid(**kwargs)
        return self

    def place(self, **kwargs):
        self.bar.place(**kwargs)
        return self

    def stop(self) -> None:
        self._alive = False
        self._release()

    def destroy(self) -> None:
        self.stop()
        try:
            self.bar.destroy()
        except Exception:
            pass
