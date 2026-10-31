"""dashboard.py -- the live training dashboard (v2 / M3).

Why this module exists: until v2 the launcher redrew a *whole matplotlib figure*
on the main thread every 2 s (``poll_training_status`` -> ``_redraw_graph``).
That is seconds-per-minute of pure UI cost competing with training, and it is
exactly what the roadmap forbids.  Everything here is pure tkinter/Canvas, owns
its own data buffer, and draws in a handful of Canvas items -- cheap enough to
sit on the 2 s status hot path (the M3 gate, ``tools/dashboard_check.py``,
asserts < 1 % of one core).

matplotlib is still bundled, but only for the explicit "Expanded chart" dialog
(``pokemon_launcher_gui.open_chart``); nothing in this module imports it, so the
hot path can never accidentally pull it in.

The widgets follow the motion spec (ROADMAP §3): the sparkline morphs over
120 ms easeInOutQuad with no axis relayout, and component bars tween over 200 ms
easeOutCubic.  Under performance mode / reduce-motion every update snaps -- the
state always applies, only the presentation is skipped.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

import theme as T
import anim as A

__all__ = ["Sparkline", "KpiCard", "ComponentBars", "ResourceStrip"]


# ---------------------------------------------------------------------------
# Sparkline
# ---------------------------------------------------------------------------

class Sparkline:
    """A pure-tk Canvas sparkline that owns its data buffer.

    Redraws a smooth line over a soft vertical gradient fill, with a fixed
    y-scale during a morph so the curve never jitters.  No axes, no relayout.
    """

    BANDS = 16        # gradient strips (more = smoother, still cheap)
    PAD_TOP = 6
    PAD_X = 4

    def __init__(self, parent, width: int = 420, height: int = 88,
                 max_points: int = 120, motion: A.Motion | None = None,
                 line: str = "chart_line", fill: str = "accent_soft",
                 bg: str = "chart_bg", grid: str = "chart_grid"):
        import tkinter as tk
        self.motion = motion or A.MOTION
        self.width = width
        self.height = height
        self.max_points = max_points
        self.canvas = tk.Canvas(parent, width=width, height=height,
                                highlightthickness=0, bd=0, bg=T.col(bg))
        self.values: list[float] = []
        self._drawn: list[float] = []
        self._scale = (0.0, 1.0)
        self._colors = {"line": T.col(line), "fill": T.col(fill),
                        "bg": T.col(bg), "grid": T.col(grid)}
        self._anim = A.Animator(self.canvas, self.motion)
        self.canvas.bind("<Configure>", lambda _e: self._render(self.values, self._scale))

    # -- layout passthrough -------------------------------------------------
    def pack(self, **kw):
        self.canvas.pack(**kw)
        return self

    def grid(self, **kw):
        self.canvas.grid(**kw)
        return self

    def destroy(self):
        try:
            self._anim.stop_all()
            self.canvas.destroy()
        except Exception:
            pass

    # -- data ---------------------------------------------------------------
    @staticmethod
    def _scale_of(values: list[float]) -> tuple[float, float]:
        if not values:
            return 0.0, 1.0
        lo, hi = min(values), max(values)
        if hi - lo < 1e-9:
            lo, hi = lo - 0.5, hi + 0.5
        return lo, hi

    @staticmethod
    def _align(old: list[float], n: int):
        if len(old) == n:
            return list(old)
        if len(old) + 1 == n and old:
            return list(old) + [old[-1]]
        return None

    def set_data(self, values) -> None:
        values = list(values)[-self.max_points:]
        self.values = values
        self._scale = self._scale_of(values)

        if len(values) < 2:
            self._render(values, self._scale)
            self._drawn = list(values)
            return

        old = self._align(self._drawn, len(values)) if self.motion.enabled else None
        if old is None:
            self._render(values, self._scale)
            self._drawn = list(values)
            return

        row = A.spec("sparkline")

        def blend(t):
            self._render([a + (b - a) * t for a, b in zip(old, values)], self._scale)

        self._anim.tween(blend, 0.0, 1.0, duration=row["ms"], easing=row["easing"],
                         tag="spark")
        self._drawn = list(values)

    def push(self, value: float) -> None:
        self.set_data(list(self.values) + [float(value)])

    def clear(self) -> None:
        self.values = []
        self._drawn = []
        self._scale = (0.0, 1.0)
        self._render([], self._scale)

    # -- drawing ------------------------------------------------------------
    def _polyline(self, values, W, H):
        lo, hi = self._scale
        base = H - self.PAD_TOP
        span = max(1e-9, hi - lo)
        usable = max(1.0, base - self.PAD_TOP)
        pts = []
        for i, v in enumerate(values):
            x = self.PAD_X + (W - 2 * self.PAD_X) * i / (len(values) - 1)
            y = base - usable * (v - lo) / span
            pts.append((x, max(self.PAD_TOP, min(base, y))))
        return pts, base

    def _render(self, values, scale) -> None:
        self._scale = scale
        c = self.canvas
        try:
            c.delete("all")
        except Exception:
            return
        W = max(10, c.winfo_width() or self.width)
        H = max(10, c.winfo_height() or self.height)
        base = H - self.PAD_TOP
        c.create_line(self.PAD_X, base, W - self.PAD_X, base, fill=self._colors["grid"])

        if len(values) < 2:
            return
        pts, base = self._polyline(values, W, H)

        # Vertical gradient: one strip per band, top (opaque) -> bottom (bg).
        top0 = self.PAD_TOP
        for j in range(self.BANDS):
            top = top0 + (base - top0) * j / self.BANDS
            bottom = top0 + (base - top0) * (j + 1) / self.BANDS
            colour = A.lerp_color(self._colors["fill"], self._colors["bg"],
                                  (j + 0.5) / self.BANDS)
            poly = []
            for x, y in pts:
                poly.extend((x, min(max(y, top), bottom)))
            poly.extend((pts[-1][0], bottom, pts[0][0], bottom))
            c.create_polygon(poly, fill=colour, outline="")

        flat = [coord for p in pts for coord in p]
        c.create_line(*flat, fill=self._colors["line"], width=2, smooth=True)
        x, y = pts[-1]
        c.create_oval(x - 2, y - 2, x + 2, y + 2,
                      fill=self._colors["line"], outline="")

    def retheme(self) -> None:
        """Re-read colours after a theme change and redraw."""
        self._colors = {"line": T.col("chart_line"), "fill": T.col("accent_soft"),
                        "bg": T.col("chart_bg"), "grid": T.col("chart_grid")}
        self._render(self.values, self._scale)


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------

class KpiCard:
    """One metric: a caption and a big value that can count up (TweenNumber)."""

    def __init__(self, parent, caption: str, value: str = "--", width: int = 104,
                 height: int = 54, decimals: int = 0, prefix: str = "",
                 suffix: str = "", motion: A.Motion | None = None):
        import customtkinter as ctk
        self.motion = motion or A.MOTION
        self.decimals = decimals
        self.frame = ctk.CTkFrame(parent, corner_radius=T.RADIUS["md"],
                                  border_width=1, border_color=T.c("border"),
                                  fg_color=T.c("card"))
        self.frame.grid_propagate(False)
        self.frame.configure(width=width, height=height)
        self.caption = ctk.CTkLabel(self.frame, text=caption, font=T.f("tiny"),
                                    text_color=T.c("text_faint"), anchor="w")
        self.caption.pack(anchor="w", padx=T.SPACE["sm"], pady=(T.SPACE["sm"], 0))
        self.value = ctk.CTkLabel(self.frame, text=value, font=T.f("h4", "bold"),
                                  text_color=T.c("text"), anchor="w")
        self.value.pack(anchor="w", padx=T.SPACE["sm"], pady=(0, T.SPACE["sm"]))
        self._num = A.TweenNumber(self.value, duration=A.spec("number")["ms"],
                                  decimals=decimals, motion=self.motion,
                                  prefix=prefix, suffix=suffix)
        self._text = value

    def set_number(self, value: float) -> None:
        self._num.set(float(value))

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        try:
            self.value.configure(text=text)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Reward-component bars
# ---------------------------------------------------------------------------

class ComponentBars:
    """Reward breakdown from ``training_status.json["components"]``.

    Rows are created lazily (the component set is not known until the first
    status file).  Each bar's width tweens; a green/red delta badge shows the
    change versus the previous sample.
    """

    def __init__(self, parent, motion: A.Motion | None = None, bar_width: int = 140):
        import customtkinter as ctk
        self.motion = motion or A.MOTION
        self.bar_width = bar_width
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.frame.grid_columnconfigure(1, weight=1)
        self._rows: dict[str, dict] = {}
        self._empty = ctk.CTkLabel(self.frame, text="no reward components yet",
                                   font=T.f("small"), text_color=T.c("text_faint"))
        self._empty.grid(row=0, column=0, columnspan=4, sticky="w")

    def pack(self, **kw):
        self.frame.pack(**kw)
        return self

    def _make_row(self, key: str, r: int) -> dict:
        import customtkinter as ctk
        label = ctk.CTkLabel(self.frame, text=key.replace("_", " ").title(),
                             font=T.f("small"), anchor="w", width=110)
        label.grid(row=r, column=0, sticky="w", pady=1)
        bar = ctk.CTkProgressBar(self.frame, width=self.bar_width, height=6,
                                 corner_radius=3, progress_color=T.c("accent"),
                                 fg_color=T.c("card"))
        bar.set(0.0)
        bar.grid(row=r, column=1, sticky="ew", padx=T.SPACE["sm"])
        value = ctk.CTkLabel(self.frame, text="", font=T.f("small"),
                             text_color=T.c("text_dim"), width=48, anchor="e")
        value.grid(row=r, column=2, sticky="e")
        delta = ctk.CTkLabel(self.frame, text="", font=T.f("small"),
                             text_color=T.c("text_faint"), width=52, anchor="e")
        delta.grid(row=r, column=3, sticky="e", padx=(T.SPACE["sm"], 0))
        return {"label": label, "bar": bar, "value": value, "delta": delta,
                "prev": None, "animator": A.Animator(bar, self.motion),
                "vtext": None, "colour": None, "target": None}

    def update(self, components: dict) -> None:
        if not components:
            return
        if self._empty is not None:
            try:
                self._empty.destroy()
            except Exception:
                pass
            self._empty = None
        scale = max([abs(float(v)) for v in components.values()] + [1e-6])
        row_spec = A.spec("component")
        for r, (key, raw) in enumerate(components.items()):
            val = float(raw)
            row = self._rows.get(key)
            if row is None:
                row = self._make_row(key, r)
                self._rows[key] = row

            # Only touch a widget when its displayed value actually changed:
            # customtkinter redraws a Canvas per configure(), which is the whole
            # per-poll cost, and most samples repeat the previous text/colour.
            vtext = f"{val:+.2f}"
            if row["vtext"] != vtext:
                row["value"].configure(text=vtext)
                row["vtext"] = vtext

            prev = row["prev"]
            row["prev"] = val
            if prev is not None and abs(val - prev) > 1e-9:
                d = val - prev
                up = d > 0
                row["delta"].configure(text=("+ " if up else "- ") + f"{abs(d):.2f}",
                                       text_color=T.col("ok") if up else T.col("danger"))

            colour = T.col("ok") if val >= 0 else T.col("danger")
            if row["colour"] != colour:
                row["bar"].configure(progress_color=colour)
                row["colour"] = colour

            target = max(0.0, min(1.0, abs(val) / scale))
            if row["target"] is not None and abs(row["target"] - target) < 1e-9:
                continue
            row["target"] = target
            if not self.motion.enabled:
                row["bar"].set(target)
            else:
                row["animator"].tween(lambda t, b=row["bar"]: b.set(t),
                                      row["bar"].get(), target,
                                      duration=row_spec["ms"], easing=row_spec["easing"],
                                      tag="bar")

    def destroy(self):
        for row in self._rows.values():
            try:
                row["animator"].stop_all()
            except Exception:
                pass
        try:
            self.frame.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Resource strip
# ---------------------------------------------------------------------------

class _GpuProbe:
    """Optional ``nvidia-smi`` probe on a background thread (cached, no hot path).

    Spawning nvidia-smi costs tens of ms, so it must never run on the 2 s status
    tick.  If the binary is missing or errors, the strip degrades to "GPU n/a".
    """

    def __init__(self, interval: float = 15.0):
        self.text: str | None = None
        self.available = bool(shutil.which("nvidia-smi"))
        self._stop = False
        if self.available:
            threading.Thread(target=self._loop, args=(interval,), daemon=True).start()

    def _loop(self, interval: float) -> None:
        while not self._stop:
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=4).stdout.strip()
                line = out.splitlines()[0] if out else ""
                if not line:
                    self.available = False
                    return
                util, used, total = [p.strip() for p in line.split(",")[:3]]
                self.text = (f"GPU {util}%  {int(used) / 1024:.1f}/{int(total) / 1024:.1f} GB")
            except Exception:
                self.available = False
                return
            time.sleep(interval)

    def stop(self) -> None:
        self._stop = True


class ResourceStrip:
    """CPU / RAM (psutil) + optional GPU (nvidia-smi) as one faint text line."""

    def __init__(self, parent, interval_ms: int = 2000):
        import customtkinter as ctk
        self.label = ctk.CTkLabel(parent, text="", font=T.f("small"),
                                  text_color=T.col("text_faint"), anchor="w")
        self._gpu = _GpuProbe()
        self._psutil = None
        try:
            import psutil
            self._psutil = psutil
            psutil.cpu_percent(None)  # prime: the first call always reads 0.0
        except Exception:
            self._psutil = None

    def pack(self, **kw):
        self.label.pack(**kw)
        return self

    def refresh(self) -> None:
        parts = []
        if self._psutil is not None:
            try:
                cpu = self._psutil.cpu_percent(None)
                vm = self._psutil.virtual_memory()
                parts.append(f"CPU {cpu:.0f}%")
                parts.append(f"RAM {vm.used / 1e9:.1f}/{vm.total / 1e9:.1f} GB")
            except Exception:
                parts.append("CPU n/a")
        else:
            parts.append("CPU n/a")
        parts.append(self._gpu.text or "GPU n/a")
        try:
            self.label.configure(text="     ".join(parts))
        except Exception:
            pass

    def stop(self) -> None:
        self._gpu.stop()

    def destroy(self):
        self.stop()
        try:
            self.label.destroy()
        except Exception:
            pass
