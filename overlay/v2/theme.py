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
    "TOKENS", "SIZES", "ACCENTS", "SPACE", "RADIUS",
    "c", "col", "f", "sizes", "set_appearance", "get_appearance",
    "set_accent", "get_accent", "apply", "enable_dpi_awareness",
    "asset_path", "assets_dir", "load_font_files", "font_family",
    "as_dict",
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
    global _appearance
    mode = (mode or "dark").lower()
    if mode == "system":
        try:
            import customtkinter as ctk
            mode = "light" if ctk.get_appearance_mode().lower() == "light" else "dark"
        except Exception:
            mode = "dark"
    if mode not in ("light", "dark"):
        raise ValueError("appearance must be 'light', 'dark' or 'system'")
    _appearance = mode
    try:
        import customtkinter as ctk
        ctk.set_appearance_mode(mode)
    except Exception:
        pass


def get_accent() -> str:
    return _accent


def set_accent(name: str) -> None:
    global _accent
    name = (name or "blue").lower()
    if name not in ACCENTS:
        raise KeyError(f"unknown accent {name!r} (known: {sorted(ACCENTS)})")
    _accent = name


def apply(appearance: str | None = None, accent: str | None = None) -> None:
    """Configure customtkinter + this module.  Safe to call more than once.

    Keeps CTk's own "blue" widget theme so frames/entries/buttons stay exactly
    as in 1.0.0; the accent presets above only recolour our explicit tokens.
    """
    if accent is not None:
        set_accent(accent)
    if appearance is not None:
        set_appearance(appearance)
    try:
        import customtkinter as ctk
        ctk.set_default_color_theme("blue")
    except Exception:
        pass


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
