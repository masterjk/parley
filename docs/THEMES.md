# Themes

parley ships several color themes. The active theme colors the whole UI
consistently — status bar, pane-border labels, the Captain prompt, the discuss
panel, and the copy popup.

## Switching themes

The active theme is stored in a config file (`$XDG_CONFIG_HOME/parley/theme`,
i.e. `~/.config/parley/theme`) and is the single source of truth.

**Live, from inside parley** — in the Captain pane:

```
/theme            # list themes, mark the active one
/theme <name>     # switch (Tab-completes names)
```

Switching writes the choice back to the config file and re-applies immediately:
the prompt and pane-border labels recolor at once; the status bar and the
discuss panel pick up the change within ~3s (their refresh throttle).

**From the shell** — edit the file or use the resolver directly:

```bash
python3 bin/parley_theme.py set mono       # persist
python3 bin/parley_theme.py current        # print active
python3 bin/parley_theme.py list           # list (marks active)
python3 bin/parley_theme.py preview         # color swatches for every theme
python3 bin/parley_theme.py preview hivis   # swatches for one theme
```

## Bundled themes

| Name        | Look |
|-------------|------|
| `harbor`    | Default. Warm accents (salmon Claude, teal Codex, pink) on charcoal. |
| `terminal`  | Maps roles to the 16 ANSI colors, so parley inherits *your* terminal palette. |
| `daybreak`  | For light terminal backgrounds: dark text, darker-hued accents. |
| `mono`      | Grayscale UI; a single cyan accent reserved for live/active state. |
| `hivis`     | High-contrast, colorblind-safe (blue/orange agents, not red/green). |
| `synthwave` | Neon magenta/cyan/violet on near-black. |

## How it works

`bin/parley_theme.py` is the single source of truth. A *theme* maps a fixed set
of semantic **roles** to color specs; every component asks for a role, never a
raw color. Roles:

```
bg text dim separator title version
claude codex accent ready init time effort mode
```

A color spec is `"<color> [attr...]"` where `<color>` is a 256-palette index
(`209`), a hex string (`#ff8770`), an ANSI name (`brightcyan`), or `default`
(the terminal's own fg/bg); attrs include `bold`, `dim`, `italic`, `underline`,
`reverse`.

The resolver emits three forms from that one definition:

- **shell escapes** with the background composited in — `parley_theme.py
  --shell-export`, consumed by `status.sh`.
- **foreground-only escapes** — `from parley_theme import palette`, consumed by
  the Python TUIs (`discuss_panel.py`, `relay.py`).
- **tmux / prompt_toolkit tokens** — `parley_theme.py --tmux-export` and
  `--pane-border-format`, for pane borders, the copy popup, and the prompt.

## Adding a theme

Add an entry to `THEMES` in `bin/parley_theme.py` defining all roles, then run
`python3 bin/parley_theme.py preview <name>` to eyeball it. The role set is
validated at import — a missing role raises immediately.

## Known limitation

The copy-success popup's color is baked into its tmux key binding at launch, so
a live `/theme` switch recolors everything *except* the popup, which picks up
the new color on the next `parley` launch. (Everything else updates live.)
