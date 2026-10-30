"""theme.py -- design tokens for the launcher UI (v2 / M0).

The point of this module is that *nothing* in the UI hardcodes a colour, size or
font any more.  M0 deliberately keeps the 1.0.0 look byte-identical: every token
below resolves, in dark mode with the default "blue" accent, to exactly the
literal it replaced (``#3b8ed0``, ``"gray70"``, ``CTkFont(size=13)``, ...).

Usage
-----
    import theme as T

    T.c("accent")        # value to hand to customtkinter (a hex string or a
                         # (light, dark) tuple -- CTk resolves tuples itself)
    T.col("accent")      # concrete hex for the *current* appearance, for plain
                         # tk widgets, Canvas and matplotlib
    T.f(13, "bold")      # a CTkFont built from the type scale
    T.SIZES["chart_tick"]# numeric sizes for things matplotlib wants as numbers

Nothing here imports customtkinter at module import time: the installer's CLI
path and the test tools must be able to read tokens without a GUI stack.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = [
    "TOKENS", "SIZES", "ACCENTS", "ACCENT_LABELS", "SPACE", "RADIUS",
    "c", "col", "f", "sizes", "set_appearance", "get_appearance",
    "set_accent", "get_accent", "accent_names", "accent_label",
    "accent_from_label", "apply", "enable_dpi_awareness",
    "asset_path", "assets_dir", "load_font_files", "font_family",
    "as_dict", "system_dpi", "dpi_scaling", "scaling_for_dpi",
    "window_scale", "screen_size", "fit_size", "set_widget_scaling",
    "set_screen_override",
]

# ---------------------------------------------------------------------------
# Colour tokens
# ---------------------------------------------------------------------------
# Each value is either a plain string, or a (light, dark) pair.  CTk accepts
# both forms for fg_color/text_color/border_color; col() resolves a pair to the
# concrete value for the current appearance.
#
# Dark values marked  == 1.0.0 are *identical* to the literal that used to be
# inline in pokemon_launcher_gui.py, so the default theme cannot change the look.

TOKENS: dict[str, object] = {
    # accent family -- overridden by the presets in ACCENTS
    "accent":        ("#1f6aa5", "#3b8ed0"),   # dark == 1.0.0
    "accent_hover":  ("#144870", "#1f6aa5"),   # dark == 1.0.0
    "on_accent":     ("#ffffff", "#ffffff"),
    "accent_soft":   ("#d7e8f7", "#1d3f5c"),   # tint fill (M3 cards)

    # text
    "text":          ("gray10", "gray90"),
    "text_dim":      ("gray40", "gray70"),     # dark == 1.0.0
    "text_faint":    ("gray50", "gray60"),     # dark == 1.0.0

    # surfaces / lines
    "border":        ("gray75", "gray25"),     # dark == 1.0.0
    "card":          ("gray92", "gray17"),     # CTkFrame default, made explicit

    # semantic
    "ok":            "#5ac47a",                # == 1.0.0
    "warn":          "#e0a15a",                # == 1.0.0
    "danger":        "#e05a5a",                # == 1.0.0
    "danger_hover":  ("#b8443f", "#c94a4a"),

    # emulator host / charts.  These are handed to matplotlib, which does NOT
    # understand customtkinter/Tk colour names -- keep them concrete hex.
    "grid_bg":       ("#e8e8e8", "#000000"),   # dark == 1.0.0 ("black")
    "chart_bg":      ("#f4f4f4", "#2b2b2b"),   # dark == 1.0.0
    "chart_grid":    ("#c8c8c8", "#555555"),   # dark == 1.0.0
    "chart_axis":    ("#6f6f6f", "#808080"),   # dark == 1.0.0 (matplotlib "gray")
    "chart_line":    ("#1f6aa5", "#3b8ed0"),   # dark == 1.0.0

    # toast / status feedback
    "toast_info":    ("gray85", "gray20"),
    "toast_ok":      ("#e3f5e7", "#1e3a26"),
    "toast_warn":    ("#fbf1e0", "#3a2f1c"),
    "toast_error":   ("#fbe6e6", "#3a1e1e"),
}

# Type scale.  chart_* are plain ints because matplotlib wants numbers.
SIZES: dict[str, int] = {
    "h1": 24,          # app title
    "h2": 18,          # dialog titles
    "h3": 17,          # dialog titles (env picker / install)
    "h4": 16,          # sub heads
    "timer": 28,       # headless timer
    "timer_grid": 20,  # grid toolbar timer
    "body": 13,
    "body_bold": 14,   # section / button labels
    "small": 11,       # help text, status lines
    "tiny": 10,
    "chart_tick": 7,   # matplotlib tick labelsize
    "chart_title": 8,  # matplotlib title fontsize
}

# Spacing / radius scales (used from M1 onwards; defined now so the token
# surface is stable).
SPACE = {"xs": 4, "sm": 8, "md": 14, "lg": 20, "xl": 24}
RADIUS = {"sm": 8, "md": 10, "lg": 12, "card": 14, "pill": 999}

# Accent presets: name -> (accent, hover, on_accent) as (light, dark) pairs.
ACCENTS: dict[str, dict[str, tuple[str, str]]] = {
    "blue": {  # == 1.0.0 default
        "accent":       ("#1f6aa5", "#3b8ed0"),
        "accent_hover": ("#144870", "#1f6aa5"),
        "accent_soft":  ("#d7e8f7", "#1d3f5c"),
        "on_accent":    ("#ffffff", "#ffffff"),
    },
    "poke_red": {
        "accent":       ("#d63a2f", "#e35d4f"),
        "accent_hover": ("#a82b22", "#c74a3d"),
        "accent_soft":  ("#fbe0dd", "#4a2320"),
        "on_accent":    ("#ffffff", "#ffffff"),
    },
    "gold": {
        "accent":       ("#a8791a", "#e5b53c"),
        "accent_hover": ("#8a6213", "#c99a2b"),
        "accent_soft":  ("#f7ecd2", "#3f331a"),
        "on_accent":    ("#ffffff", "#1a1a1a"),
    },
    "green": {
        "accent":       ("#2e8b57", "#43c07a"),
        "accent_hover": ("#236b43", "#37a165"),
        "accent_soft":  ("#dff2e6", "#1f3b2c"),
        "on_accent":    ("#ffffff", "#0f1f16"),
    },
    "dmg": {  # original Game Boy greens
        "accent":       ("#4f7a20", "#9bbc0f"),
        "accent_hover": ("#3d5f19", "#8bac0f"),
        "accent_soft":  ("#e8efd0", "#1f2a12"),
        "on_accent":    ("#ffffff", "#0f1408"),
    },
}

# Display order + human labels for the in-app accent switcher (M1).
ACCENT_LABELS = {
    "blue": "Blue",
    "poke_red": "Poké Red",
    "gold": "Gold",
    "green": "Green",
    "dmg": "DMG",
}

_appearance = "dark"
_accent = "blue"

_gray_cache: dict[str, str] = {}


def _named_hex(name: str):
    """Resolve a Tk/X11 colour name (e.g. 'gray25') to hex, or None.

    Tk's own colour database is authoritative when a window exists, so results
    match what customtkinter renders.  Without a root we fall back to the linear
    grayN mapping; the small rounding differences only affect non-GUI tooling.
    """
    if name in _gray_cache:
        return _gray_cache[name]
    result = None
    try:
        import tkinter as tk
        root = getattr(tk, "_default_root", None)
        if root is not None:
            r, g, b = root.winfo_rgb(name)  # 16-bit per channel
            result = "#%02x%02x%02x" % (r >> 8, g >> 8, b >> 8)
    except Exception:
        result = None
    if result is None:
        s = name.lower()
        if s.startswith("gray") and s[4:].isdigit():
            v = min(255, int(int(s[4:]) * 255 / 100 + 0.5))
            result = "#%02x%02x%02x" % (v, v, v)
        elif s == "gray":
            result = "#bebebe"
    if result:
        _gray_cache[name] = result
    return result

# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

_LIGHT_INDEX, _DARK_INDEX = 0, 1


def _is_pair(value) -> bool:
    return isinstance(value, (tuple, list)) and len(value) == 2


def _token(name: str):
    """Raw token value, with the active accent preset applied."""
    if name in ACCENTS["blue"] and name in ACCENTS[_accent]:
        return ACCENTS[_accent][name]
    try:
        return TOKENS[name]
    except KeyError:
        raise KeyError(f"unknown theme token {name!r} (known: {sorted(TOKENS)})") from None


def c(name: str):
    """Value for a customtkinter widget (hex string, or (light, dark) tuple)."""
    return _token(name)


def col(name: str) -> str:
    """Concrete colour for the current appearance (plain tk / Canvas / matplotlib).

    Named Tk grays are resolved to hex so the result is always valid everywhere,
    including matplotlib (which rejects names like ``gray70``).
    """
    value = _token(name)
    if _is_pair(value):
        value = value[_DARK_INDEX] if _appearance == "dark" else value[_LIGHT_INDEX]
    if isinstance(value, str) and value[:1].isalpha():
        return _named_hex(value) or value
    return value


def f(size=13, weight: str = "normal"):
    """A CTkFont from the type scale.  ``size`` may be an int or a SIZES key.

    Family is None unless a font has actually been bundled *and* registered
    (see load_font_files): customtkinter's own default (Roboto) is what 1.0.0
    shipped, so choosing a different system font here would be a visual change.
    """
    import customtkinter as ctk

    n = SIZES[size] if isinstance(size, str) else int(size)
    return ctk.CTkFont(family=font_family(), size=n, weight=weight)


def sizes(key: str) -> int:
    return SIZES[key]


# ---------------------------------------------------------------------------
# Theme state
# ---------------------------------------------------------------------------

def get_appearance() -> str:
    return _appearance


def set_appearance(mode: str) -> None:
    """Set the appearance.  ``mode`` may be 'light', 'dark' or 'system'.

    'system' is handed to customtkinter unchanged so it genuinely follows the
    OS setting; ``_appearance`` stores the *resolved* light/dark value that
    :func:`col` needs.
    """
    global _appearance
    mode = (mode or "dark").lower()
    if mode not in ("light", "dark", "system"):
        raise ValueError("appearance must be 'light', 'dark' or 'system'")
    resolved = "light" if mode == "light" else "dark"
    try:
        import customtkinter as ctk
        ctk.set_appearance_mode(mode)  # customtkinter understands "system"
        resolved = "light" if ctk.get_appearance_mode().lower() == "light" else "dark"
    except Exception:
        pass
    _appearance = resolved


def get_accent() -> str:
    return _accent


def set_accent(name: str) -> None:
    global _accent
    name = (name or "blue").lower()
    if name not in ACCENTS:
        raise KeyError(f"unknown accent {name!r} (known: {sorted(ACCENTS)})")
    _accent = name


def accent_names() -> list[str]:
    """Accent preset ids, in the order the switcher should show them."""
    return [n for n in ACCENT_LABELS if n in ACCENTS]


def accent_label(name: str) -> str:
    """Human label for a preset id (falls back to the id itself)."""
    return ACCENT_LABELS.get(name, name)


def accent_from_label(label: str) -> str:
    """Inverse of :func:`accent_label`, always returning a valid preset id."""
    for name, text in ACCENT_LABELS.items():
        if text == label:
            return name
    return "blue"


def _retint_ctk_theme() -> None:
    """Point customtkinter's stock controls at the current accent preset.

    ``blue`` is customtkinter's own theme, so it is deliberately left alone --
    dark+blue therefore stays byte-identical to 1.0.0.  Every other preset
    recolours the widgets customtkinter would otherwise draw blue (buttons,
    checkboxes, radios, sliders, scrollbars, ...) so a red/gold/DMG accent looks
    finished instead of blue-with-red-bars.

    Call it *after* ``ctk.set_default_color_theme`` and *before* building
    widgets (customtkinter reads ``ThemeManager.theme`` at construction time, so
    the accent switcher rebuilds the screen right after this runs).
    """
    if _accent == "blue":
        return
    try:
        import customtkinter as ctk
        theme = ctk.ThemeManager.theme
    except Exception:
        return
    if not theme:
        return
    fg, hover, on = c("accent"), c("accent_hover"), c("on_accent")
    updates = {
        "CTkButton":          {"fg_color": fg, "hover_color": hover, "text_color": on},
        "CTkCheckBox":        {"fg_color": fg, "hover_color": hover, "checkmark_color": on},
        "CTkRadioButton":     {"fg_color": fg, "hover_color": hover},
        "CTkSegmentedButton": {"selected_color": fg, "selected_hover_color": hover},
        "CTkOptionMenu":      {"fg_color": fg, "button_color": fg,
                               "button_hover_color": hover, "text_color": on},
        "CTkComboBox":        {"button_color": fg, "button_hover_color": hover},
        "CTkProgressBar":     {"progress_color": fg},
        "CTkSlider":          {"progress_color": fg, "button_color": fg,
                               "button_hover_color": hover},
        "CTkSwitch":          {"progress_color": fg},
        "CTkScrollbar":       {"button_color": fg, "button_hover_color": hover},
    }
    for key, mapping in updates.items():
        node = theme.get(key)
        if isinstance(node, dict):
            node.update(mapping)


def apply(appearance: str | None = None, accent: str | None = None) -> None:
    """Configure customtkinter + this module.  Safe to call more than once.

    The classic "blue" accent keeps customtkinter's own widget theme, so
    dark+blue is byte-identical to 1.0.0; the other accent presets recolour both
    our tokens and customtkinter's stock controls (see :func:`_retint_ctk_theme`).
    """
    if accent is not None:
        set_accent(accent)
    if appearance is not None:
        set_appearance(appearance)
    try:
        import customtkinter as ctk
        ctk.set_default_color_theme("blue")  # resets ThemeManager.theme
    except Exception:
        pass
    _retint_ctk_theme()


def as_dict() -> dict:
    """Flattened snapshot for tools/tests: concrete colours for both modes."""
    out = {"appearance": _appearance, "accent": _accent, "colors": {}, "sizes": dict(SIZES)}
    for name, value in TOKENS.items():
        if _is_pair(value):
            out["colors"][name] = {"light": value[_LIGHT_INDEX], "dark": value[_DARK_INDEX]}
        else:
            out["colors"][name] = value
    return out


# ---------------------------------------------------------------------------
# DPI
# ---------------------------------------------------------------------------

def enable_dpi_awareness() -> str:
    """Make the process per-monitor DPI aware so the window isn't blurry.

    Must run before the first Tk window is created.  Returns which API worked,
    or "none" (non-Windows / failure) -- callers must not depend on it.
    """
    if not sys.platform.startswith("win"):
        return "none"
    import ctypes
    # PROCESS_PER_MONITOR_DPI_AWARE = 2 (shcore, Win 8.1+), best quality
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per_monitor_v1"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


def scaling_for_dpi(dpi: int = 96) -> float:
    """Widget-scaling factor for a DPI value (CTk defaults to 96 dpi == 1.0)."""
    return max(0.5, min(3.0, dpi / 96.0))


# Test seams: the screenshot harness emulates a different display scaling and/or
# a smaller screen on this one monitor.  None means "use the real value".
_scaling_override: float | None = None
_screen_override: tuple[int, int] | None = None


def system_dpi() -> int:
    """DPI of the primary monitor (96 == 100 % scaling)."""
    if not sys.platform.startswith("win"):
        return 96
    import ctypes
    try:
        return int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        pass
    try:
        import tkinter as tk
        root = getattr(tk, "_default_root", None)
        if root is not None:
            return int(round(root.winfo_fpixels("1i")))
    except Exception:
        pass
    return 96


def dpi_scaling() -> float:
    """Scaling factor of the primary monitor (1.25 at 125 %, ...)."""
    return scaling_for_dpi(system_dpi())


def window_scale(widget) -> float:
    """Factor customtkinter is scaling *this* window's geometry by.

    customtkinter multiplies a requested ``WxH`` by its window scaling (which
    tracks the monitor DPI); ``+x+y`` is left in physical pixels.  Tk's own
    pixels-per-inch for the window equals that factor times 96, so we can read
    it without reaching into customtkinter's internals.  When the screenshot
    harness has emulated a different scaling, that value wins.
    """
    if _scaling_override is not None:
        return _scaling_override
    try:
        return max(0.5, float(widget.winfo_fpixels("1i")) / 96.0)
    except Exception:
        return dpi_scaling()


def screen_size(widget=None) -> tuple[int, int]:
    """Physical screen size, honouring the harness' ``set_screen_override``.

    ``widget`` may be None (uses the primary monitor / override).
    """
    if _screen_override is not None:
        return _screen_override
    try:
        return widget.winfo_screenwidth(), widget.winfo_screenheight()
    except Exception:
        pass
    try:
        import tkinter as tk
        root = getattr(tk, "_default_root", None)
        if root is not None:
            return root.winfo_screenwidth(), root.winfo_screenheight()
    except Exception:
        pass
    return 0, 0


def fit_size(widget, width: int, height: int,
             margin_x: int = 24, margin_y: int = 80) -> tuple[int, int]:
    """Clamp a requested *logical* window size so it fits the screen at this DPI.

    ``geometry("1000x760")`` becomes 1250x950 physical at 125 %, so a size that
    is fine on paper can hang off a 720-tall screen.  Returns the largest size
    that still leaves room for the taskbar/title bar.
    """
    scale = window_scale(widget)
    try:
        screen_w, screen_h = screen_size(widget)
        screen_w = screen_w / scale
        screen_h = screen_h / scale
    except Exception:
        return width, height
    return (int(min(width, max(320, screen_w - margin_x))),
            int(min(height, max(240, screen_h - margin_y))))


def set_screen_override(width: int | None, height: int | None = None) -> None:
    """Pretend the screen is ``width``x``height`` *physical* pixels (harness only).

    Lets the screenshot tool assert the layout against a small display (e.g.
    1280x720) without changing the monitor.  ``None`` restores the real screen.
    """
    global _screen_override
    _screen_override = None if not width else (int(width), int(height or width))


def set_widget_scaling(factor: float | None = None) -> None:
    """Emulate a display scaling of ``factor`` (1.0 == 100 %, 1.5 == 150 %).

    ``None`` clears the emulation (customtkinter scaling back to 1.0).  customtkinter
    multiplies its window scaling *on top of* the monitor DPI factor, so we cancel
    the real factor and ask for the target one; the emulated factor is remembered so
    :func:`window_scale` (and therefore :func:`fit_size`) stays consistent.
    This exists for the screenshot harness -- real use relies on per-monitor
    automatic scaling.
    """
    global _scaling_override
    import customtkinter as ctk
    if factor is None:
        _scaling_override = None
        ctk.set_widget_scaling(1.0)
        ctk.set_window_scaling(1.0)
        return
    factor = float(factor)
    _scaling_override = factor
    rel = factor / max(0.1, dpi_scaling())
    ctk.set_widget_scaling(rel)
    ctk.set_window_scaling(rel)


# ---------------------------------------------------------------------------
# Assets + fonts
# ---------------------------------------------------------------------------

def assets_dir() -> Path:
    """Folder holding bundled assets, frozen or not."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base) / "assets"
    return Path(__file__).resolve().parent / "assets"


def asset_path(name: str) -> Path | None:
    """Path to a bundled asset, or None if it isn't there (callers fall back)."""
    p = assets_dir() / name
    return p if p.exists() else None


_FONT_DIRS = ("fonts",)
_font_family_cache: dict[str, str | None] = {}
_bundled_loaded = False
UI_FONT_CANDIDATES = ("Inter", "Segoe UI", "Tahoma")
MONO_FONT_CANDIDATES = ("JetBrains Mono", "Cascadia Mono", "Consolas", "Courier New")


def load_font_files() -> list[str]:
    """Register any bundled .ttf/.otf in assets/fonts with the OS.

    Returns the file names that registered.  Bundling fonts is optional -- when
    nothing is bundled this is a no-op and the system/CTk default is used.
    """
    global _bundled_loaded
    if not sys.platform.startswith("win"):
        return []
    import ctypes
    added: list[str] = []
    for sub in _FONT_DIRS:
        d = assets_dir() / sub
        if not d.is_dir():
            continue
        for path in sorted(d.iterdir()):
            if path.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                continue
            try:
                # FR_PRIVATE = 0x10: usable by this process without installing.
                if ctypes.windll.gdi32.AddFontResourceExW(str(path), 0x10, 0):
                    added.append(path.name)
            except Exception:
                pass
    if added:
        _bundled_loaded = True
        _font_family_cache.clear()
    return added


def font_family(kind: str = "ui") -> str | None:
    """Family name for CTkFont, or None to keep customtkinter's default.

    None until a font is bundled (``overlay/v2/assets/fonts/*.ttf``), which is
    exactly the 1.0.0 behaviour.
    """
    if not _bundled_loaded:
        return None
    if kind in _font_family_cache:
        return _font_family_cache[kind]
    family: str | None = None
    try:
        from tkinter import font as tkfont
        available = {n.lower() for n in tkfont.families()}
        for cand in (MONO_FONT_CANDIDATES if kind == "mono" else UI_FONT_CANDIDATES):
            if cand.lower() in available:
                family = cand
                break
    except Exception:
        family = None
    _font_family_cache[kind] = family
    return family
