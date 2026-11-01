"""tiles.py -- emulator grid chrome (v2 / M4).

The launcher owns the *frame* around each PyBoy SDL window, never its pixels:
``embed_window`` reparents the SDL HWND under a plain ``tk.Frame`` we control
(ROADMAP §1).  This module builds that frame -- a titled tile with an accent
border on hover -- and owns the geometry rules that follow from it:

* tiles live in a Tk grid, so **layout is Tk's job** (no per-frame geometry
  loop, no 60 fps tile physics);
* the embedded child is a *native* window, so Tk cannot place it -- we snap it
  with ``SetWindowPos`` into the largest centred **10:9 (Game Boy)** rectangle
  inside the tile body, on every body ``<Configure>`` (ROADMAP §3: retile is
  "geometry snaps; only the outline animates");
* only colours animate (the accent border, 90 ms linear).  Geometry never does.

Everything degrades gracefully.  With no ``win32gui`` (a headless test host) or
before any window has appeared, tiles still lay out and show a placeholder body;
a tile whose window never arrived stays **stale with a Retry** instead of one
global status line (ROADMAP §2D).
"""

from __future__ import annotations

import math
import tkinter as tk

import customtkinter as ctk

import anim as A
import theme as T

__all__ = [
    "Tile", "TileGrid", "letterbox", "shape_for", "window_title",
    "PRESETS", "ASPECT",
]

ASPECT = (10, 9)  # Game Boy 160 x 144

# Layout presets: (label, tiles per page).  0 == "show every tile".
PRESETS = (("All", 0), ("1", 1), ("2", 2), ("4", 4), ("8", 8), ("12", 12))


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def letterbox(w, h, aspect=ASPECT, margin=0):
    """The largest centred ``aspect`` (>=1) rectangle inside ``w x h``.

    Returns ``(x, y, w, h)`` in the parent's client coordinates -- exactly what
    ``SetWindowPos`` wants for a reparented child window.
    """
    aw, ah = aspect
    w = max(0, int(w) - 2 * margin)
    h = max(0, int(h) - 2 * margin)
    if w <= 0 or h <= 0:
        return margin, margin, max(1, w), max(1, h)
    if w * ah > h * aw:            # wider than the aspect -> height-bound
        nw, nh = max(1, h * aw // ah), h
    else:
        nw, nh = w, max(1, w * ah // aw)
    return margin + (w - nw) // 2, margin + (h - nh) // 2, nw, nh


def shape_for(n):
    """Grid shape ``(cols, rows)`` for ``n`` equal tiles.

    Hand-picked up to 12 (1x1, 2x1, 3x1, 2x2, 3x2, 4x2, 3x3, 4x3), then
    square-ish packing.  The picks keep the cells close to the 10:9 tile aspect
    and, where possible, leave no empty slot (8 -> 4x2, not 3x3).
    """
    preferred = {
        1: (1, 1), 2: (2, 1), 3: (3, 1), 4: (2, 2), 5: (3, 2), 6: (3, 2),
        7: (4, 2), 8: (4, 2), 9: (3, 3), 10: (4, 3), 11: (4, 3), 12: (4, 3),
    }
    if n in preferred:
        return preferred[n]
    cols = int(math.ceil(math.sqrt(n)))
    return cols, int(math.ceil(n / cols))


# ---------------------------------------------------------------------------
# win32 (lazy: the module must import on a host without pywin32)
# ---------------------------------------------------------------------------

def _wg():
    try:
        import win32gui
        return win32gui
    except Exception:
        return None


def _wc():
    try:
        import win32con
        return win32con
    except Exception:
        return None


def window_title(hwnd) -> str:
    """The embedded window's title, or "" when win32/the window is unavailable."""
    wg = _wg()
    if wg is None:
        return ""
    try:
        return (wg.GetWindowText(hwnd) or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# One tile
# ---------------------------------------------------------------------------

class Tile:
    """One emulator cell: header chrome + a 10:9 body the SDL window snaps into."""

    def __init__(self, parent, index, grid, embed_fn=None):
        self.grid = grid
        self.index = index
        self.hwnd = None
        self.title = ""
        self.stale = True
        self.reason = "Waiting for window"
        self.hero = False
        self.shown = False
        self._embed_fn = embed_fn
        self._hover = False
        self._animator = None
        self._build(parent)

    # -- construction -------------------------------------------------------
    def _build(self, parent):
        self.frame = ctk.CTkFrame(parent, corner_radius=T.RADIUS["md"], border_width=1,
                                  border_color=T.c("border"), fg_color=T.c("card"))
        self._animator = A.Animator(self.frame)

        header = ctk.CTkFrame(self.frame, fg_color="transparent")
        header.pack(fill="x", padx=T.SPACE["sm"], pady=(T.SPACE["xs"], 0))
        self.header = header

        self.cpu_label = ctk.CTkLabel(header, text=f"CPU {self.index + 1}",
                                      font=T.f("small", "bold"), anchor="w")
        self.cpu_label.pack(side="left")
        self.title_label = ctk.CTkLabel(header, text="", font=T.f("tiny"),
                                        text_color=T.c("text_dim"), anchor="w")
        self.title_label.pack(side="left", padx=(T.SPACE["sm"], 0))

        chips = ctk.CTkFrame(header, fg_color="transparent")
        chips.pack(side="right")
        self.chips = chips
        # Order matters: pack(side="right") stacks leftwards, so copy ends up
        # right-most, then zoom, and Retry (packed only when stale) left-most.
        self.copy_btn = ctk.CTkButton(chips, text="Copy", width=48, height=20,
                                      corner_radius=T.RADIUS["sm"], font=T.f("tiny"),
                                      fg_color="transparent", hover_color=T.c("accent_soft"),
                                      text_color=T.c("text_dim"), command=self._on_copy)
        self.copy_btn.pack(side="right", padx=(T.SPACE["xs"], 0))
        self.zoom_btn = ctk.CTkButton(chips, text="Zoom", width=52, height=20,
                                      corner_radius=T.RADIUS["sm"], font=T.f("tiny"),
                                      fg_color="transparent", hover_color=T.c("accent_soft"),
                                      text_color=T.c("text_dim"), command=self._on_zoom)
        self.zoom_btn.pack(side="right", padx=(T.SPACE["xs"], 0))
        self.retry_btn = ctk.CTkButton(chips, text="Retry", width=54, height=20,
                                       corner_radius=T.RADIUS["sm"], font=T.f("tiny"),
                                       fg_color=T.c("warn"), hover_color=T.c("accent_hover"),
                                       command=self._on_retry)

        self.stage = tk.Frame(self.frame, bg=T.col("grid_bg"), highlightthickness=0, bd=0)
        self.stage.pack(fill="both", expand=True,
                        padx=T.SPACE["xs"], pady=T.SPACE["xs"])
        self.stage.bind("<Configure>", lambda e: self.fit())

        self.placeholder = ctk.CTkLabel(self.stage, text="", font=T.f("tiny"),
                                        text_color=T.c("text_faint"), fg_color=T.c("grid_bg"))
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")

        for widget in (self.frame, header, self.cpu_label, self.title_label, self.stage):
            widget.bind("<Enter>", lambda e: self._hover_in(), add="+")
            widget.bind("<Leave>", lambda e: self._hover_out(), add="+")
        for widget in (header, self.cpu_label, self.title_label):
            widget.bind("<Button-1>", lambda e: self.grid.set_hero(self), add="+")
        try:
            header.configure(cursor="hand2")
        except Exception:
            pass

    # -- hover / focus border (the only animated thing here) ----------------
    def _hover_in(self):
        self._hover = True
        self._tint(True)

    def _hover_out(self):
        self._hover = False
        self._tint(False)

    def _tint(self, active):
        target = T.c("accent") if (active or self.hero) else T.c("border")
        row = A.spec("tile_border")
        try:
            self._animator.tween_props({"border_color": target}, duration=row["ms"],
                                       easing=row["easing"],
                                       tag=f"tile-border:{self.index}")
        except Exception:
            pass

    # -- state --------------------------------------------------------------
    def assign(self, hwnd, title=""):
        """Bind ``hwnd`` to this tile and embed it into the body."""
        self.hwnd = hwnd
        self.title = (title or "").strip()
        embedded = self._embed_into_stage()
        self.mark_live()
        return embedded

    def mark_live(self):
        self.stale = False
        self.reason = ""
        try:
            self.title_label.configure(text=self.title or "running",
                                       text_color=T.c("text_dim"))
        except Exception:
            pass
        self.retry_btn.pack_forget()
        try:
            self.placeholder.configure(text="")
        except Exception:
            pass
        self._tint(self._hover)

    def mark_stale(self, reason="Waiting for window"):
        self.stale = True
        self.reason = reason
        try:
            self.title_label.configure(text=reason, text_color=T.c("warn"))
        except Exception:
            pass
        try:
            if not self.retry_btn.winfo_ismapped():
                self.retry_btn.pack(side="right", padx=(T.SPACE["xs"], 0))
        except Exception:
            pass
        try:
            self.placeholder.configure(text=f"CPU {self.index + 1}\n{reason}")
            self.placeholder.lift()
        except Exception:
            pass
        self._tint(self._hover)

    def set_shown(self, visible):
        """Show/hide the native child when the tile leaves/joins the page."""
        self.shown = bool(visible)
        if self.hwnd:
            wg, wc = _wg(), _wc()
            if wg is not None and wc is not None:
                try:
                    wg.ShowWindow(self.hwnd, wc.SW_SHOW if self.shown else wc.SW_HIDE)
                except Exception:
                    pass
        if self.shown:
            self.fit()

    # -- geometry -----------------------------------------------------------
    def _embed_into_stage(self):
        if not self.hwnd or self._embed_fn is None:
            return False
        try:
            self.stage.update_idletasks()
            sw = self.stage.winfo_width()
            sh = self.stage.winfo_height()
        except Exception:
            sw = sh = 64
        x, y, w, h = letterbox(sw, sh)
        try:
            self._embed_fn(self.hwnd, self.stage.winfo_id(), x, y, w, h)
            return True
        except Exception as e:  # noqa: BLE001 - a floating window beats a crash
            self.mark_stale(f"embed failed: {e}")
            return False

    def fit(self):
        """Snap the embedded child to the current 10:9 body rect (Configure)."""
        if not self.hwnd or self.stale or not self.shown:
            return
        wg = _wg()
        if wg is None:
            return
        try:
            if not wg.IsWindow(self.hwnd):
                return
            self.stage.update_idletasks()
            sw = self.stage.winfo_width()
            sh = self.stage.winfo_height()
        except Exception:
            return
        if sw < 8 or sh < 8:
            return
        x, y, w, h = letterbox(sw, sh)
        wc = _wc()
        flags = (wc.SWP_NOZORDER | wc.SWP_NOACTIVATE) if wc is not None else 0
        try:
            wg.SetWindowPos(self.hwnd, 0, x, y, w, h, flags)
        except Exception:
            pass

    # -- chip handlers ------------------------------------------------------
    def _on_zoom(self):
        self.grid.set_hero(self)

    def _on_copy(self):
        self.grid.copy_tile(self)

    def _on_retry(self):
        self.grid.retry_tile(self)

    def destroy(self):
        try:
            self._animator.stop_all()
        except Exception:
            pass
        try:
            self.frame.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------

class TileGrid:
    """Owns every tile, the layout presets, hero mode and the stale/retry state.

    ``parent`` must be a widget that supports ``grid`` (the launcher packs a
    plain ``tk.Frame`` game area for exactly this).  ``embed_fn`` is the host's
    ``embed_window(hwnd, parent_hwnd, x, y, w, h)`` so all win32 style surgery
    stays in one place.
    """

    def __init__(self, parent, expected, embed_fn=None, on_copy=None, on_retry=None):
        self.parent = parent
        self.expected = max(0, int(expected))
        self._embed_fn = embed_fn
        self._on_copy = on_copy
        self._on_retry = on_retry
        self.tiles: list[Tile] = []
        self.preset = 0          # tiles per page; 0 == show everything
        self.page = 0
        self.hero_tile = None
        self._cols = 0
        self._rows = 0
        for _ in range(self.expected):
            self._add_tile()

    # -- tiles --------------------------------------------------------------
    def _add_tile(self):
        tile = Tile(self.parent, len(self.tiles), self, self._embed_fn)
        self.tiles.append(tile)
        return tile

    def ensure(self, n):
        while len(self.tiles) < n:
            self._add_tile()

    def hwnds(self):
        return {t.hwnd for t in self.tiles if t.hwnd}

    def live_count(self):
        return sum(1 for t in self.tiles if t.hwnd and not t.stale)

    def stale_count(self):
        return sum(1 for t in self.tiles if t.stale)

    def tile_for_hwnd(self, hwnd):
        for tile in self.tiles:
            if tile.hwnd == hwnd:
                return tile
        return None

    # -- paging / presets ---------------------------------------------------
    def per_page(self):
        n = max(1, len(self.tiles))
        if self.preset <= 0:
            return n
        return min(self.preset, n)

    def pages(self):
        return max(1, int(math.ceil(len(self.tiles) / self.per_page())))

    def set_preset(self, per_page):
        self.preset = int(per_page)
        self.page = 0
        self.hero_tile = None
        for tile in self.tiles:
            tile.hero = False
        self.layout()

    def next_page(self, delta):
        total = self.pages()
        if total <= 1:
            return
        self.page = (self.page + delta) % total
        self.layout()

    # -- hero ---------------------------------------------------------------
    def set_hero(self, tile):
        if self.hero_tile is tile:
            self.clear_hero()
            return
        self.hero_tile = tile
        for t in self.tiles:
            t.hero = (t is tile)
            t._tint(t._hover)
        self.page = 0
        self.layout()

    def clear_hero(self):
        if self.hero_tile is None:
            return
        self.hero_tile = None
        for t in self.tiles:
            t.hero = False
            t._tint(t._hover)
        self.layout()

    # -- layout -------------------------------------------------------------
    def layout(self):
        if not self.tiles:
            return
        if self.hero_tile is not None:
            self._layout_hero()
            return
        per_page = self.per_page()
        cols, rows = shape_for(per_page)
        start = self.page * per_page
        end = start + per_page
        self._reset_weights(max(cols, self._cols), max(rows, self._rows))
        for c in range(cols):
            self.parent.grid_columnconfigure(c, weight=1, uniform="tilecol")
        for r in range(rows):
            self.parent.grid_rowconfigure(r, weight=1, uniform="tilerow")
        self._cols, self._rows = cols, rows
        for i, tile in enumerate(self.tiles):
            if start <= i < end:
                r, c = divmod(i - start, cols)
                tile.frame.grid(row=r, column=c, sticky="nsew",
                                padx=(0 if c == 0 else T.SPACE["xs"], 0),
                                pady=(0 if r == 0 else T.SPACE["xs"], 0))
                tile.set_shown(True)
            else:
                tile.frame.grid_remove()
                tile.set_shown(False)

    def _layout_hero(self):
        self._reset_weights(max(1, self._cols), max(1, self._rows))
        self.parent.grid_columnconfigure(0, weight=1)
        self.parent.grid_rowconfigure(0, weight=1)
        self._cols, self._rows = 1, 1
        for tile in self.tiles:
            if tile is self.hero_tile:
                tile.frame.grid(row=0, column=0, sticky="nsew")
                tile.set_shown(True)
            else:
                tile.frame.grid_remove()
                tile.set_shown(False)

    def _reset_weights(self, cols, rows):
        for c in range(max(cols, 1)):
            try:
                self.parent.grid_columnconfigure(c, weight=0, uniform="")
            except Exception:
                pass
        for r in range(max(rows, 1)):
            try:
                self.parent.grid_rowconfigure(r, weight=0, uniform="")
            except Exception:
                pass

    def refit_all(self):
        for tile in self.tiles:
            if tile.shown:
                tile.fit()

    # -- assignment / watchdog ---------------------------------------------
    def assign_many(self, hwnds):
        """Bind discovered hwnds to free tiles, in order.  Returns how many."""
        known = self.hwnds()
        bound = 0
        for hwnd in hwnds:
            if hwnd in known:
                continue
            free = next((t for t in self.tiles if t.hwnd is None), None)
            if free is None:
                self.ensure(len(self.tiles) + 1)
                free = self.tiles[-1]
            free.assign(hwnd, window_title(hwnd))
            known.add(hwnd)
            bound += 1
        if bound:
            self.layout()
        return bound

    def mark_dead(self, hwnds, reason="Window closed"):
        """Mark the tiles owning any of ``hwnds`` stale (window vanished)."""
        changed = 0
        wg, wc = _wg(), _wc()
        for tile in self.tiles:
            if tile.hwnd in hwnds:
                hwnd = tile.hwnd
                tile.hwnd = None
                tile.mark_stale(reason)
                if wg is not None and wc is not None and hwnd:
                    try:
                        wg.ShowWindow(hwnd, wc.SW_HIDE)
                    except Exception:
                        pass
                changed += 1
        return changed

    def destroy(self):
        for tile in self.tiles:
            tile.destroy()
        self.tiles = []

    # -- host callbacks -----------------------------------------------------
    def copy_tile(self, tile):
        if self._on_copy is not None:
            self._on_copy(tile)

    def retry_tile(self, tile):
        if self._on_retry is not None:
            self._on_retry(tile)
