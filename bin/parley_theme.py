#!/usr/bin/env python3
"""Themes for parley.

Single source of truth for parley's color palette. A *theme* maps a fixed set
of semantic roles (claude, codex, accent, ready, ...) to color specs. Every
component asks for a role, never a raw color, so swapping the theme recolors
the whole UI consistently.

Consumers and the form each needs:
  * status.sh   -> `eval "$(parley_theme.py --shell-export)"` gives ready-to-use
                   escape sequences, background composited in.
  * Python TUIs -> `from parley_theme import palette; p = palette()` gives
                   foreground-only escapes (panes paint on their own bg).
  * tmux / pt   -> `parley_theme.py --tmux-export` gives tmux color tokens
                   (and prompt_toolkit style strings) for borders/popup/prompt.

The active theme is a single name stored at `config_dir()/theme` (see
parley_paths). It is the source of truth: `/theme <name>` in the relay rewrites
it and re-applies live. Default is "harbor" (parley's original look).

Color spec grammar (per role): "<color> [attr...]"
  <color> : a 256-palette index (0-255), a hex string (#rrggbb), or an ANSI
            name (black red green yellow blue magenta cyan white, each with an
            optional "bright" prefix) or "default" (the terminal's own fg/bg).
  attr    : bold dim italic underline reverse
e.g. "209", "#ff8770 bold", "brightcyan bold", "default".
"""
from __future__ import annotations

import sys
from pathlib import Path

from parley_paths import config_dir, ensure_runtime_dirs

# ---------------------------------------------------------------------------
# Roles. The contract every theme must satisfy. Adding a role here means every
# theme below must define it (validated at import via _validate).
# ---------------------------------------------------------------------------
ROLES = (
    "bg",         # status-bar background
    "text",       # default foreground
    "dim",        # de-emphasized (parenthesized versions, etc.)
    "separator",  # │ dividers
    "title",      # app name, prompt, pane-border labels
    "version",    # version number
    "claude",     # Claude identity (label, border, discuss speaker)
    "codex",      # Codex identity
    "accent",     # discuss state, spinner, active discuss border
    "ready",      # READY state, copy-success popup
    "init",       # INIT state
    "time",       # clock
    "edit",       # edit-owner name
    "effort",     # effort tier
    "mode",       # fast-mode flag
)

DEFAULT_THEME = "harbor"

# ---------------------------------------------------------------------------
# Themes. Each is a full role->spec map. Tune freely; the role names are the
# stable contract, the colors are not.
# ---------------------------------------------------------------------------
THEMES: dict[str, dict[str, str]] = {
    # The original look: warm accents on charcoal.
    "harbor": {
        "bg": "235", "text": "252", "dim": "244", "separator": "240",
        "title": "87 bold", "version": "67",
        "claude": "209", "codex": "79", "accent": "213 bold",
        "ready": "120 bold", "init": "215 bold", "time": "221",
        "edit": "228 bold", "effort": "180", "mode": "213 bold",
    },
    # Inherit the terminal's own 16-color palette: parley wears whatever theme
    # the terminal already uses. "default" = the terminal's fg/bg.
    "terminal": {
        "bg": "default", "text": "default", "dim": "brightblack",
        "separator": "brightblack",
        "title": "cyan bold", "version": "blue",
        "claude": "magenta", "codex": "cyan", "accent": "blue bold",
        "ready": "green bold", "init": "yellow bold", "time": "yellow",
        "edit": "yellow bold", "effort": "white", "mode": "magenta bold",
    },
    # Light theme: pure-white background, dark text, darker-hued accents that
    # stay legible on white.
    "daybreak": {
        "bg": "#ffffff", "text": "236", "dim": "245", "separator": "250",
        "title": "26 bold", "version": "61",
        "claude": "124", "codex": "30", "accent": "92 bold",
        "ready": "28 bold", "init": "166 bold", "time": "94",
        "edit": "100 bold", "effort": "95", "mode": "92 bold",
    },
    # Grayscale UI; a single cyan accent reserved for live/active state.
    "mono": {
        "bg": "234", "text": "250", "dim": "240", "separator": "238",
        "title": "252 bold", "version": "245",
        "claude": "252", "codex": "247", "accent": "39 bold",
        "ready": "39 bold", "init": "39 bold", "time": "245",
        "edit": "252 bold", "effort": "244", "mode": "39 bold",
    },
    # High-contrast, colorblind-safe: blue/orange agents (not red/green),
    # white text on near-black.
    "hivis": {
        "bg": "16", "text": "231", "dim": "250", "separator": "244",
        "title": "51 bold", "version": "45",
        "claude": "33 bold", "codex": "208 bold", "accent": "201 bold",
        "ready": "45 bold", "init": "226 bold", "time": "231",
        "edit": "226 bold", "effort": "250", "mode": "201 bold",
    },
    # Neon on near-black.
    "synthwave": {
        "bg": "233", "text": "219", "dim": "96", "separator": "91",
        "title": "51 bold", "version": "141",
        "claude": "213", "codex": "51", "accent": "201 bold",
        "ready": "48 bold", "init": "214 bold", "time": "219",
        "edit": "226 bold", "effort": "141", "mode": "201 bold",
    },
}

# ---------------------------------------------------------------------------
# Color spec parsing + conversion.
# ---------------------------------------------------------------------------
ESC = "\033"

# ANSI base names -> (fg SGR code, bg SGR code, tmux name, prompt_toolkit name)
_ANSI = {
    "black":   (30, 40, "black",   "ansiblack"),
    "red":     (31, 41, "red",     "ansired"),
    "green":   (32, 42, "green",   "ansigreen"),
    "yellow":  (33, 43, "yellow",  "ansiyellow"),
    "blue":    (34, 44, "blue",    "ansiblue"),
    "magenta": (35, 45, "magenta", "ansimagenta"),
    "cyan":    (36, 46, "cyan",    "ansicyan"),
    "white":   (37, 47, "white",   "ansiwhite"),
}
_BRIGHT = {
    "brightblack":   (90, 100, "brightblack",   "ansibrightblack"),
    "brightred":     (91, 101, "brightred",     "ansibrightred"),
    "brightgreen":   (92, 102, "brightgreen",   "ansibrightgreen"),
    "brightyellow":  (93, 103, "brightyellow",  "ansibrightyellow"),
    "brightblue":    (94, 104, "brightblue",    "ansibrightblue"),
    "brightmagenta": (95, 105, "brightmagenta", "ansibrightmagenta"),
    "brightcyan":    (96, 106, "brightcyan",    "ansibrightcyan"),
    "brightwhite":   (97, 107, "brightwhite",   "ansibrightwhite"),
}
_ANSI_ALL = {**_ANSI, **_BRIGHT}

_ATTR_SGR = {
    "bold": "1", "dim": "2", "italic": "3", "underline": "4", "reverse": "7",
}
# prompt_toolkit attribute spellings (it has its own).
_ATTR_PT = {
    "bold": "bold", "dim": "", "italic": "italic", "underline": "underline",
    "reverse": "reverse",
}
# tmux attribute spellings.
_ATTR_TMUX = {
    "bold": "bold", "dim": "dim", "italic": "italics", "underline": "underscore",
    "reverse": "reverse",
}


def _parse(spec: str) -> tuple[str, list[str]]:
    """'209 bold' -> ('209', ['bold'])."""
    parts = spec.split()
    if not parts:
        raise ValueError("empty color spec")
    return parts[0], parts[1:]


def _index_to_hex(i: int) -> str:
    """xterm 256-palette index -> #rrggbb."""
    if 0 <= i <= 15:
        # Standard 16. Use the common xterm values.
        base = [
            "000000", "800000", "008000", "808000", "000080", "800080",
            "008080", "c0c0c0", "808080", "ff0000", "00ff00", "ffff00",
            "0000ff", "ff00ff", "00ffff", "ffffff",
        ]
        return "#" + base[i]
    if 16 <= i <= 231:
        n = i - 16
        r, g, b = n // 36, (n // 6) % 6, n % 6
        conv = lambda v: 0 if v == 0 else 55 + 40 * v
        return f"#{conv(r):02x}{conv(g):02x}{conv(b):02x}"
    # 232-255 grayscale ramp.
    v = 8 + (i - 232) * 10
    return f"#{v:02x}{v:02x}{v:02x}"


def _is_index(token: str) -> bool:
    return token.isdigit() and 0 <= int(token) <= 255


# --- SGR (terminal escape) ---------------------------------------------------

def _fg_sgr_params(token: str) -> str:
    if token == "default":
        return "39"
    if _is_index(token):
        return f"38;5;{token}"
    if token.startswith("#"):
        r, g, b = (int(token[i:i + 2], 16) for i in (1, 3, 5))
        return f"38;2;{r};{g};{b}"
    if token in _ANSI_ALL:
        return str(_ANSI_ALL[token][0])
    raise ValueError(f"bad color: {token}")


def _bg_sgr_params(token: str) -> str:
    if token == "default":
        return "49"
    if _is_index(token):
        return f"48;5;{token}"
    if token.startswith("#"):
        r, g, b = (int(token[i:i + 2], 16) for i in (1, 3, 5))
        return f"48;2;{r};{g};{b}"
    if token in _ANSI_ALL:
        return str(_ANSI_ALL[token][1])
    raise ValueError(f"bad color: {token}")


def _attr_params(attrs: list[str], table: dict[str, str]) -> list[str]:
    out = []
    for a in attrs:
        v = table.get(a)
        if v:
            out.append(v)
    return out


def fg_escape(spec: str) -> str:
    """Foreground-only escape for Python TUIs painting on their own bg."""
    token, attrs = _parse(spec)
    params = _attr_params(attrs, _ATTR_SGR) + [_fg_sgr_params(token)]
    return f"{ESC}[{';'.join(params)}m"


def composite_escape(bg_spec: str, spec: str) -> str:
    """bg + fg escape, for the status bar (every cell sits on the theme bg)."""
    bg_token, _ = _parse(bg_spec)
    token, attrs = _parse(spec)
    params = [_bg_sgr_params(bg_token)]
    params += _attr_params(attrs, _ATTR_SGR)
    params.append(_fg_sgr_params(token))
    return f"{ESC}[{';'.join(params)}m"


# --- tmux / prompt_toolkit ---------------------------------------------------

def tmux_color(spec: str) -> str:
    token, _ = _parse(spec)
    if token == "default":
        return "default"
    if _is_index(token):
        return f"colour{token}"
    if token.startswith("#"):
        return token
    if token in _ANSI_ALL:
        return _ANSI_ALL[token][2]
    raise ValueError(f"bad color: {token}")


def tmux_style(spec: str, *, with_bg: str | None = None) -> str:
    """tmux style string, e.g. 'fg=colour87,bold' (+ optional bg)."""
    token, attrs = _parse(spec)
    bits = [f"fg={tmux_color(spec)}"]
    if with_bg is not None:
        bits.append(f"bg={tmux_color(with_bg)}")
    bits += _attr_params(attrs, _ATTR_TMUX)
    return ",".join(bits)


def pt_color(spec: str) -> str:
    """prompt_toolkit style string, e.g. 'ansicyan bold' or '#ff8770 bold'.

    prompt_toolkit takes hex or ansi* names, not colour<N>, so 256-indexes are
    converted to hex.
    """
    token, attrs = _parse(spec)
    if token == "default":
        color = "default"
    elif _is_index(token):
        color = _index_to_hex(int(token))
    elif token.startswith("#"):
        color = token
    elif token in _ANSI_ALL:
        color = _ANSI_ALL[token][3]
    else:
        raise ValueError(f"bad color: {token}")
    parts = [color] + _attr_params(attrs, _ATTR_PT)
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Theme resolution + active-theme config.
# ---------------------------------------------------------------------------

def _validate() -> None:
    for name, theme in THEMES.items():
        missing = [r for r in ROLES if r not in theme]
        if missing:
            raise ValueError(f"theme {name!r} missing roles: {missing}")
        for role, spec in theme.items():
            try:
                _fg_sgr_params(_parse(spec)[0])
            except ValueError as e:
                raise ValueError(f"theme {name!r} role {role!r}: {e}") from e


_validate()


def theme_file() -> Path:
    return config_dir() / "theme"


def list_themes() -> list[str]:
    # Default first, then the rest alphabetically.
    rest = sorted(n for n in THEMES if n != DEFAULT_THEME)
    return [DEFAULT_THEME] + rest


def current_theme() -> str:
    """Active theme name from the config file; DEFAULT_THEME if unset/invalid."""
    try:
        name = theme_file().read_text().strip()
    except (FileNotFoundError, OSError):
        return DEFAULT_THEME
    return name if name in THEMES else DEFAULT_THEME


def set_theme(name: str) -> bool:
    """Persist the active theme. Returns False for an unknown name."""
    if name not in THEMES:
        return False
    ensure_runtime_dirs()
    theme_file().write_text(name + "\n")
    return True


def resolve(name: str | None = None) -> dict[str, str]:
    """Role -> spec for the named theme (or the active one)."""
    return THEMES[name if name in THEMES else current_theme()]


def palette(name: str | None = None) -> dict[str, str]:
    """Role -> foreground-only escape, for Python TUIs. Includes 'reset'."""
    theme = resolve(name)
    p = {role: fg_escape(theme[role]) for role in ROLES}
    p["reset"] = f"{ESC}[0m"
    return p


# ---------------------------------------------------------------------------
# Export forms.
# ---------------------------------------------------------------------------

# status.sh variable name -> role. These names match status.sh's historical
# locals so the shell side is a near drop-in.
_STATUS_VARS = {
    "TITLE": "title", "VER": "version", "SEP": "separator",
    "CLAUDECOL": "claude", "CODEXCOL": "codex", "TIMECOL": "time",
    "EDITCOL": "edit", "EFFORTCOL": "effort", "MODECOL": "mode",
    "DIMCOL": "dim", "DEF": "text", "READY": "ready", "DISC": "accent",
    "INIT": "init",
}


def shell_export(name: str | None = None) -> str:
    """Escape sequences for status.sh, background composited into each."""
    theme = resolve(name)
    bg = theme["bg"]
    bg_token, _ = _parse(bg)
    lines = [
        f"RESET='{ESC}[0m'",
        f"BG='{ESC}[{_bg_sgr_params(bg_token)}m'",
    ]
    for var, role in _STATUS_VARS.items():
        lines.append(f"{var}='{composite_escape(bg, theme[role])}'")
    return "\n".join(lines)


def pane_border_format(name: str | None = None) -> str:
    """tmux pane-border-format string with the theme's title color baked in.

    Used by both start.sh (at launch) and relay.py (on a live /theme switch) so
    the [CLAUDE]/[CODEX]/[CAPTAIN]/[DISCUSS] labels recolor with the theme. Panes
    with no @parley_label (the status strip) render a blank border.
    """
    style = tmux_style(resolve(name)["title"])  # e.g. "fg=colour87,bold"
    return ("#{?#{==:#{@parley_label},},,"
            "#[align=left," + style + "] #{@parley_label} #[default]}")


def tmux_export(name: str | None = None) -> str:
    """tmux color tokens + prompt_toolkit strings for borders/popup/prompt."""
    theme = resolve(name)
    out = [
        f"PARLEY_TMUX_TITLE='{tmux_color(theme['title'])}'",
        f"PARLEY_TMUX_CLAUDE='{tmux_color(theme['claude'])}'",
        f"PARLEY_TMUX_CODEX='{tmux_color(theme['codex'])}'",
        f"PARLEY_TMUX_ACCENT='{tmux_color(theme['accent'])}'",
        # Copy popup: a 'ready'-colored chip. Text uses the theme background
        # color, which is designed to contrast with the bright ready accent in
        # both dark and light themes.
        f"PARLEY_TMUX_POPUP_BG='{tmux_color(theme['ready'])}'",
        f"PARLEY_TMUX_POPUP_FG='{tmux_color(theme['bg'])}'",
        f"PARLEY_PT_PROMPT='{pt_color(theme['title'])}'",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def _preview(name: str) -> None:
    theme = resolve(name)
    bg = theme["bg"]
    reset = f"{ESC}[0m"
    marker = " *" if name == current_theme() else ""
    print(f"{name}{marker}")
    for role in ROLES:
        swatch = composite_escape(bg, theme[role]) + f"  {role:<10} {theme[role]:<14}" + reset
        print("  " + swatch)
    print()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: parley_theme.py "
              "{--shell-export|--tmux-export|current|set <name>|list|names|"
              "preview [name]} [theme]", file=sys.stderr)
        return 2

    cmd = argv[0]

    if cmd == "--shell-export":
        print(shell_export(argv[1] if len(argv) > 1 else None))
        return 0
    if cmd == "--tmux-export":
        print(tmux_export(argv[1] if len(argv) > 1 else None))
        return 0
    if cmd == "--pane-border-format":
        print(pane_border_format(argv[1] if len(argv) > 1 else None))
        return 0
    if cmd == "current":
        print(current_theme())
        return 0
    if cmd == "names":
        print("\n".join(list_themes()))
        return 0
    if cmd == "list":
        cur = current_theme()
        for n in list_themes():
            print(f"{'* ' if n == cur else '  '}{n}")
        return 0
    if cmd == "set":
        if len(argv) < 2:
            print("usage: parley_theme.py set <name>", file=sys.stderr)
            return 2
        if not set_theme(argv[1]):
            print(f"unknown theme: {argv[1]} (have: {', '.join(list_themes())})",
                  file=sys.stderr)
            return 1
        print(argv[1])
        return 0
    if cmd == "preview":
        if len(argv) > 1:
            if argv[1] not in THEMES:
                print(f"unknown theme: {argv[1]}", file=sys.stderr)
                return 1
            _preview(argv[1])
        else:
            for n in list_themes():
                _preview(n)
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
