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
tmux set-option -t "$SESSION" pane-border-format \
  '#{?#{==:#{@parley_label},},,#[align=left fg=colour87 bold] #{@parley_label} #[default]}'
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
