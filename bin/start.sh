#!/usr/bin/env bash
set -euo pipefail

SESSION="parley"

# BIN_DIR = the parley install tree (libexec/bin under brew, repo/bin from
# source). Read-only assets (prompts, scripts) live here. Derived from $0, so
# it is independent of where the user invoked parley.
BIN_DIR="$(cd "$(dirname "$0")" && pwd)"

# WORK_DIR = the directory the agents operate in — the user's project. No `cd`
# runs above, so $PWD is still the caller's directory even through the
# symlink-resolving wrapper and brew shim. An optional first arg (`parley DIR`)
# overrides it.
if [ "${1:-}" != "" ] && [ -d "${1:-}" ]; then
  WORK_DIR="$(cd "$1" && pwd)"
else
  WORK_DIR="$PWD"
fi

# ---- preflight: check required tools ------------------------------------
need_tool() {
  local cmd="$1" url="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "parley: required tool '$cmd' not found on PATH." >&2
    echo "Install it: $url" >&2
    echo "Or run: parley doctor" >&2
    exit 1
  fi
}
need_tool tmux    "https://github.com/tmux/tmux/wiki/Installing"
need_tool python3 "https://www.python.org/downloads/"
need_tool claude  "https://docs.claude.com/en/docs/claude-code/overview"
need_tool codex   "https://github.com/openai/codex"

# Minimum tmux version (we use split-window flags from 3.0+).
TMUX_VER_RAW=$(tmux -V 2>/dev/null | awk '{print $2}' | sed 's/[^0-9.].*//')
TMUX_MAJOR=${TMUX_VER_RAW%%.*}
if [ -n "${TMUX_MAJOR:-}" ] && [ "$TMUX_MAJOR" -lt 3 ] 2>/dev/null; then
  echo "parley: tmux $TMUX_VER_RAW is too old (need 3.0+)." >&2
  exit 1
fi

# Python deps
if ! python3 -c 'import prompt_toolkit' 2>/dev/null; then
  echo "parley: python module 'prompt_toolkit' not installed." >&2
  echo "Install it: pip install -r '$WORK_DIR/requirements.txt'" >&2
  echo "Or run: parley doctor" >&2
  exit 1
fi
# -------------------------------------------------------------------------

# Runtime paths come from bin/parley_paths.py — single source of truth shared
# with the Python tools. Creates the dirs as a side-effect.
eval "$(python3 "$BIN_DIR/parley_paths.py" --shell-export)"

# Claude Code encodes the project's absolute path as its session directory
# name by replacing every "/" with "-". Derive it from $WORK_DIR so the tool
# works for any clone location and any user.
CLAUDE_PROJ_DIR="$HOME/.claude/projects/$(echo "$WORK_DIR" | sed 's|/|-|g')"
CODEX_SESS_DIR="$HOME/.codex/sessions"

# Snapshot existing session files BEFORE launching agents so we can identify
# the new ones each agent creates on startup.
CLAUDE_BEFORE=$(mktemp)
CODEX_BEFORE=$(mktemp)
mkdir -p "$CLAUDE_PROJ_DIR" "$CODEX_SESS_DIR"
ls "$CLAUDE_PROJ_DIR"/*.jsonl 2>/dev/null | sort > "$CLAUDE_BEFORE" || true
find "$CODEX_SESS_DIR" -name 'rollout-*.jsonl' 2>/dev/null | sort > "$CODEX_BEFORE" || true

# Clear stale read-cursors and the ready flag from previous sessions.
rm -f "$PARLEY_CURSORS_DIR/"*_read.txt "$PARLEY_AGENTS_READY"
python3 "$BIN_DIR/edit_owner.py" release --force >/dev/null 2>&1 || true

tmux kill-session -t "$SESSION" 2>/dev/null || true

# ---- pane layout --------------------------------------------------------
#
# +-----------------------------------------+
# |  STATUS (1 line)                        |
# +-----------------------------------------+
# |  Claude            |  Codex             |
# +-----------------------------------------+
# |  Captain's Pane: relay input (15 lines) |
# +-----------------------------------------+

# Start session (single pane), sized to the current terminal so that absolute
# pane heights (-l 1, -l 5) are not proportionally rescaled on attach.
# Use stty size (queries the actual tty) instead of tput which may be wrong
# when called from a non-interactive shell.
if SIZE=$(stty size < /dev/tty 2>/dev/null); then
  ROWS=${SIZE% *}
  COLS=${SIZE#* }
else
  ROWS=50; COLS=200
fi
tmux new-session -d -s "$SESSION" -x "$COLS" -y "$ROWS" -c "$WORK_DIR"
CLAUDE_PANE=$(tmux display-message -t "$SESSION" -p '#{pane_id}')

# Session-local settings: mouse click/scroll/resize + larger scrollback.
tmux set-option -t "$SESSION" mouse on
tmux set-option -t "$SESSION" history-limit 50000

# Auto-copy mouse selections to the system clipboard. tmux's default fills
# only its own paste buffer; piping the selection through the OS clipboard
# tool on drag-release makes the selection available to paste (Cmd-V / Ctrl-V)
# in other apps. Detect the clipboard command per-platform and skip silently
# if none is present so the binding never errors on a headless box. The
# bind-key is server-global (tmux has no per-session key tables), so it
# applies to copy-mode everywhere, not just the parley session.
if command -v pbcopy >/dev/null 2>&1; then
  PARLEY_CLIP="pbcopy"                              # macOS
elif command -v wl-copy >/dev/null 2>&1; then
  PARLEY_CLIP="wl-copy"                             # Linux / Wayland
elif command -v xclip >/dev/null 2>&1; then
  PARLEY_CLIP="xclip -selection clipboard"          # Linux / X11
elif command -v xsel >/dev/null 2>&1; then
  PARLEY_CLIP="xsel --clipboard --input"            # Linux / X11 (alt)
else
  PARLEY_CLIP=""
fi
if [ -n "$PARLEY_CLIP" ]; then
  # Popup colors follow the active theme (PARLEY_TMUX_POPUP_BG/FG). The popup
  # binding is baked at launch, so its color reflects the theme at start time;
  # a live /theme switch recolors the bar/panels/prompt but the popup picks up
  # the new color on the next launch.
  eval "$(python3 "$BIN_DIR/parley_theme.py" --tmux-export 2>/dev/null)" || true
  : "${PARLEY_TMUX_POPUP_BG:=green}" "${PARLEY_TMUX_POPUP_FG:=black}"

  # On a successful copy, announce it. tmux >= 3.2 has display-popup, so we
  # flash a small box right at the mouse (-x M -y M) — where the user's eyes
  # already are — far more obvious than a status-line toast. Older tmux falls
  # back to a bright bold inline-styled status message.
  #
  # Both notify forms must run *after* the copy in the SAME key binding:
  # display-popup's mouse position only resolves inside the triggering event's
  # context, so (unlike a plain toast) it can't be folded into the copy-pipe
  # shell command. Chaining two commands onto one binding from the shell is the
  # catch — "tmux bind-key ... \; <cmd>" runs <cmd> immediately instead of
  # binding it, and brace blocks don't survive shell quoting. The reliable fix
  # is to write the brace-block binding to a temp file and source-file it,
  # where tmux parses the multi-command block natively.
  case "$TMUX_VER_RAW" in
    *.*) TMUX_MINOR=${TMUX_VER_RAW#*.}; TMUX_MINOR=${TMUX_MINOR%%.*} ;;
    *)   TMUX_MINOR=0 ;;
  esac
  case "$TMUX_MINOR" in ''|*[!0-9]*) TMUX_MINOR=0 ;; esac
  TMUX_NUM=$(( ${TMUX_MAJOR:-0} * 100 + TMUX_MINOR ))
  if [ "$TMUX_NUM" -ge 304 ]; then
    # tmux 3.4+ has the popup style flags (-s content, -S border, -b lines):
    # a solid 'ready'-colored block with contrasting bold text and matching
    # rounded border, so the toast reads as a highlighted chip at the mouse.
    PARLEY_NOTIFY="display-popup -E -b rounded -s 'bg=${PARLEY_TMUX_POPUP_BG},fg=${PARLEY_TMUX_POPUP_FG},bold' -S 'fg=${PARLEY_TMUX_POPUP_FG},bg=${PARLEY_TMUX_POPUP_BG}' -w 30 -h 3 -x M -y M \"printf '  ✓  Copied to clipboard  '; sleep 0.6\""
  else
    # Older tmux (no popup, or popup without style flags): bright inline-styled
    # status message in the theme's ready colors, at the bottom.
    PARLEY_NOTIFY="display-message \"#[bg=${PARLEY_TMUX_POPUP_BG},fg=${PARLEY_TMUX_POPUP_FG},bold]  ✓  Copied to clipboard  #[default]\""
  fi
  PARLEY_COPY_CONF=$(mktemp -t parley-copy.XXXXXX)
  # Four bindings, all routed through the same clipboard tool + notify:
  #   - MouseDragEnd1Pane (copy-mode / -vi): drag-select then release.
  #   - DoubleClick1Pane (root): select the word under the cursor.
  #   - TripleClick1Pane (root): select the whole line.
  # The click bindings mirror tmux's built-in defaults (select-pane, then the
  # #{pane_in_mode}/#{mouse_any_flag} guard that passes the click through to a
  # mode or a mouse-grabbing app) and only add the clipboard pipe + notify on
  # the copy branch. We drop the default's "run-shell -d 0.3" highlight dwell:
  # it sits between select and copy and would delay the popup, and our popup is
  # the feedback now anyway — removing it also keeps the mouse position fresh
  # for the popup's -x M -y M.
  cat > "$PARLEY_COPY_CONF" <<EOF
bind-key -T copy-mode MouseDragEnd1Pane {
  send-keys -X copy-pipe-and-cancel "$PARLEY_CLIP"
  $PARLEY_NOTIFY
}
bind-key -T copy-mode-vi MouseDragEnd1Pane {
  send-keys -X copy-pipe-and-cancel "$PARLEY_CLIP"
  $PARLEY_NOTIFY
}
bind-key -T root DoubleClick1Pane {
  select-pane -t =
  if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" {
    send-keys -M
  } {
    copy-mode -H
    send-keys -X select-word
    send-keys -X copy-pipe-and-cancel "$PARLEY_CLIP"
    $PARLEY_NOTIFY
  }
}
bind-key -T root TripleClick1Pane {
  select-pane -t =
  if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" {
    send-keys -M
  } {
    copy-mode -H
    send-keys -X select-line
    send-keys -X copy-pipe-and-cancel "$PARLEY_CLIP"
    $PARLEY_NOTIFY
  }
}
EOF
  tmux source-file "$PARLEY_COPY_CONF" \
    || echo "parley: clipboard copy-notify binding failed to load" >&2
  rm -f "$PARLEY_COPY_CONF"
fi

# Status strip ABOVE Claude (-b -v), don't move focus (-d). Height 2: the top
# row is the pane border (carries the [CLAUDE]/[CODEX]/... label once
# pane-border-status is enabled below), the bottom row is status.sh output.
STATUS_PANE=$(tmux split-window -b -v -d -l 2 \
  -t "$CLAUDE_PANE" -c "$WORK_DIR" \
  -P -F '#{pane_id}' \
  "'$BIN_DIR/status.sh'")

# Relay strip below Claude (15 lines).
tmux split-window -v -l 15 -t "$CLAUDE_PANE" -c "$WORK_DIR"
RELAY_PANE=$(tmux display-message -t "$SESSION" -p '#{pane_id}')

# Codex split: right of Claude.
tmux split-window -h -t "$CLAUDE_PANE" -c "$WORK_DIR"
CODEX_PANE=$(tmux display-message -t "$SESSION" -p '#{pane_id}')

# ---- pane border labels -------------------------------------------------
# Each pane's top border carries a label identifying who lives there. We
# store the label in @parley_label (a per-pane user option) rather than the
# pane_title, because Claude/Codex TUIs emit OSC title escape sequences that
# would overwrite pane_title and clobber the [CLAUDE]/[CODEX] labels. The
# format hides the border text for panes with no label (the status strip),
# and the discussion pane sets its own @parley_label at creation time
# (see relay.py cmd_discuss).
tmux set-option -t "$SESSION" pane-border-status top
# Label color follows the active theme's title color (relay.py re-applies this
# same format on a live /theme switch). Fall back to the original if the
# resolver is unavailable.
PARLEY_BORDER_FMT="$(python3 "$BIN_DIR/parley_theme.py" --pane-border-format 2>/dev/null)"
[ -z "$PARLEY_BORDER_FMT" ] && PARLEY_BORDER_FMT='#{?#{==:#{@parley_label},},,#[align=left fg=colour87 bold] #{@parley_label} #[default]}'
tmux set-option -t "$SESSION" pane-border-format "$PARLEY_BORDER_FMT"
tmux set-option -p -t "$CLAUDE_PANE" @parley_label '[CLAUDE]'
tmux set-option -p -t "$CODEX_PANE"  @parley_label '[CODEX]'
tmux set-option -p -t "$RELAY_PANE"  @parley_label '[CAPTAIN]'

# Render the agent system-prompt files with $BIN_DIR substituted in for the
# __PARLEY_BIN__ placeholder, so commands like `peek.py` and `edit_owner.py`
# are absolute paths the agent can run from any cwd. Without this, agents
# running from the user's project dir hit "bin/peek.py: not found" and resort
# to searching the filesystem for it. sed delimiter is `|` so the slashes in
# $BIN_DIR don't conflict.
CLAUDE_PROMPT_RENDERED=$(mktemp -t parley-claude.XXXXXX)
CODEX_PROMPT_RENDERED=$(mktemp -t parley-codex.XXXXXX)
sed "s|__PARLEY_BIN__|$BIN_DIR|g" "$BIN_DIR/prompts/claude.txt" > "$CLAUDE_PROMPT_RENDERED"
sed "s|__PARLEY_BIN__|$BIN_DIR|g" "$BIN_DIR/prompts/codex.txt"  > "$CODEX_PROMPT_RENDERED"

# Start agents via respawn-pane: replaces the pane's process with the agent
# directly — no shell prompt or command echo visible to the user. Both agents
# launch with their default approval/sandbox behavior so Captain stays in the
# loop on tool calls (no --dangerously-* flags), and each gets its system
# prompt injected via the CLI's native mechanism rather than a pane paste:
#   - claude: --append-system-prompt-file extends the default system prompt
#   - codex:  -c model_instructions_file=... overlays a TOML config key
# This makes the intro invisible in the pane (no fake "user message" turn)
# and removes the previous race where pasted intro text would dismiss the
# startup trust-this-folder dialog and kill the agent process.
# Auto-restart wrapper: if the user accidentally exits the agent with Ctrl-C at
# an empty prompt (the agent's own native gesture), the pane respawns it instead
# of leaving an idle pane. The 1s sleep avoids a tight crash loop if the agent
# fails fast (auth error, etc.). Stop with `/quit` from the Captain.
tmux respawn-pane -k -t "$CLAUDE_PANE" \
  "while :; do claude --append-system-prompt-file '$CLAUDE_PROMPT_RENDERED'; sleep 1; done"
tmux respawn-pane -k -t "$CODEX_PANE" \
  "while :; do codex --no-alt-screen -c 'model_instructions_file=\"$CODEX_PROMPT_RENDERED\"'; sleep 1; done"

: > "$PARLEY_STARTUP_LOG"

# Agents are usable as soon as the respawn-pane calls return; the relay can
# open immediately. The startup trust dialog, if any, still appears in each
# pane on first launch in a new directory — Captain dismisses it manually.
# With no intro paste racing it, the dialog stays put until answered.
touch "$PARLEY_AGENTS_READY"
echo "[$(date '+%H:%M:%S')] agents launched; ready flag set" >> "$PARLEY_STARTUP_LOG"

# Pipeline B — session-file pinning runs independently, no time pressure.
# Cursor files used by peek.py and discuss.py are written when the agents
# eventually create their JSONLs. Up to 5 minutes; usually a few seconds.
(
  set +e
  exec >>"$PARLEY_STARTUP_LOG" 2>&1
  echo "[$(date '+%H:%M:%S')] session-poll: starting (background)"
  NEW_CLAUDE=""
  NEW_CODEX=""
  for i in $(seq 1 1500); do  # 1500 * 0.2s = 5 min ceiling
    NEW_CLAUDE=$(comm -13 "$CLAUDE_BEFORE" <(ls "$CLAUDE_PROJ_DIR"/*.jsonl 2>/dev/null | sort) 2>/dev/null | tail -1)
    NEW_CODEX=$(comm -13 "$CODEX_BEFORE" <(find "$CODEX_SESS_DIR" -name 'rollout-*.jsonl' 2>/dev/null | sort) 2>/dev/null | tail -1)
    if [ -n "$NEW_CLAUDE" ] && [ -n "$NEW_CODEX" ]; then
      echo "[$(date '+%H:%M:%S')] session-poll: detected after ${i} polls"
      break
    fi
    sleep 0.2
  done
  printf '%s' "$NEW_CLAUDE" > "$PARLEY_CURSORS_DIR/claude.session"
  printf '%s' "$NEW_CODEX"  > "$PARLEY_CURSORS_DIR/codex.session"
  rm -f "$CLAUDE_BEFORE" "$CODEX_BEFORE"
  echo "[$(date '+%H:%M:%S')] session-poll: claude=$NEW_CLAUDE"
  echo "[$(date '+%H:%M:%S')] session-poll: codex=$NEW_CODEX"
) &

# Start relay via respawn-pane: no shell prompt or command echo.
tmux respawn-pane -k -t "$RELAY_PANE" \
  "env CLAUDE_PANE='$CLAUDE_PANE' CODEX_PANE='$CODEX_PANE' WORK_DIR='$WORK_DIR' python3 '$BIN_DIR/relay.py'"

# Force absolute heights AFTER the client attaches (the attach can rescale
# the layout, overriding any resize-pane we ran beforehand).
(
  set +e
  exec >>"$PARLEY_STARTUP_LOG" 2>&1
  sleep 0.5  # let attach complete
  tmux resize-pane -t "$STATUS_PANE" -y 2
  tmux resize-pane -t "$RELAY_PANE"  -y 15
  echo "[$(date '+%H:%M:%S')] post-attach resize: status=1, relay=15 (term ${ROWS}x${COLS})"
) &

# Focus relay so the user lands there
tmux select-pane -t "$RELAY_PANE"
tmux attach-session -t "$SESSION"
