#!/usr/bin/env bash
# Single-line colored status bar.
# Layout: [APP v0.1]                 [time] │ [status: STATE]
# States: INIT (orange + spinner) → READY (green) → DISCUSSING (pink + spinner)
set -u

BIN_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="parley"
# Read version from the wrapper script ($BIN_DIR/../parley) so we inherit
# whatever brew's inreplace baked in at install time. Source checkouts get
# "dev"; brew installs get the tag string (e.g. "0.9.0"). Falls back to "?"
# if the call fails.
VERSION="$("$BIN_DIR/../parley" --version 2>/dev/null | awk '{print $2}')"
[ -z "$VERSION" ] && VERSION="?"

eval "$(python3 "$BIN_DIR/parley_paths.py" --shell-export)"
PID_FILE="$PARLEY_DIALOG_PID"
READY_FILE="$PARLEY_AGENTS_READY"

# CLI versions never change mid-session — snapshot once, not per frame.
CLAUDE_VER=$(claude --version 2>/dev/null | awk '{print $1}')
CODEX_VER=$(codex --version 2>/dev/null | awk '{print $NF}')

# Live model/effort/mode comes from the session JSONLs (and settings.json for
# Claude's effortLevel) and can change, but slowly — refresh on a throttle (see
# REFRESH_EVERY below) rather than every frame.
CLAUDE_MODEL=""; CLAUDE_EFFORT=""; CLAUDE_MODE=""
CODEX_MODEL=""; CODEX_EFFORT=""

# Theme palette comes from parley_theme.py (single source of truth — it emits
# RESET/BG/TITLE/VER/... as ready-to-use escapes with the theme background
# composited in). Re-loaded on the refresh throttle below, so a `/theme` switch
# recolors the bar live without a restart. RESET is hard-defaulted first so the
# cursor-restore trap stays safe even if the loader ever fails.
RESET=$'\033[0m'
load_theme() {
  eval "$(python3 "$BIN_DIR/parley_theme.py" --shell-export 2>/dev/null)" || true
}
load_theme

SPIN=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
SLEN=${#SPIN[@]}

printf '\033[?25l'
trap 'printf "\033[?25h%s\n" "$RESET"; exit 0' EXIT INT TERM

# ~3s between agent-info refreshes (REFRESH_EVERY * sleep 0.15).
REFRESH_EVERY=20
EDIT_REFRESH_EVERY=5

EDIT_OWNER="idle"; EDIT_NOTE=""; EDIT_AGE_SECONDS=0; EDIT_STALE=0; EDIT_STALE_OWNER=""

i=0
refresh=0
while true; do
  NOW=$(date '+%H:%M:%S')
  SPINNER="${SPIN[i]}"
  i=$(( (i + 1) % SLEN ))

  # Re-read live model/effort on the throttle (and on the very first frame).
  if [ $(( refresh % REFRESH_EVERY )) -eq 0 ]; then
    eval "$(python3 "$BIN_DIR/agent_info.py" 2>/dev/null)" || true
    # Pick up a live `/theme` switch on the same cadence (~3s).
    load_theme
  fi
  if [ $(( refresh % EDIT_REFRESH_EVERY )) -eq 0 ]; then
    eval "$(python3 "$BIN_DIR/edit_owner.py" status --shell 2>/dev/null)" || true
  fi
  refresh=$(( refresh + 1 ))

  # Build the agent identity block, model-first with version in parens. Effort
  # and (for Claude) fast mode are tucked between model and version:
  #   claude <model> [<effort>] [fast] (<ver>)   codex <model> [<effort>] (<ver>)
  # `normal` mode is the default and gets hidden — only `fast` surfaces.
  # Colored and plain are built in lockstep so the padding math stays exact.
  AGENTS_COLORED="   ${CLAUDECOL}claude${DEF}"
  AGENTS_PLAIN="   claude"
  if [ -n "$CLAUDE_MODEL" ]; then
    AGENTS_COLORED="$AGENTS_COLORED ${CLAUDE_MODEL}"
    AGENTS_PLAIN="$AGENTS_PLAIN ${CLAUDE_MODEL}"
  fi
  if [ -n "$CLAUDE_EFFORT" ]; then
    AGENTS_COLORED="$AGENTS_COLORED ${EFFORTCOL}${CLAUDE_EFFORT}${DEF}"
    AGENTS_PLAIN="$AGENTS_PLAIN ${CLAUDE_EFFORT}"
  fi
  if [ "$CLAUDE_MODE" = "fast" ]; then
    AGENTS_COLORED="$AGENTS_COLORED ${MODECOL}fast${DEF}"
    AGENTS_PLAIN="$AGENTS_PLAIN fast"
  fi
  AGENTS_COLORED="$AGENTS_COLORED ${DIMCOL}(${CLAUDE_VER})${DEF}"
  AGENTS_PLAIN="$AGENTS_PLAIN (${CLAUDE_VER})"

  AGENTS_COLORED="$AGENTS_COLORED   ${CODEXCOL}codex${DEF}"
  AGENTS_PLAIN="$AGENTS_PLAIN   codex"
  if [ -n "$CODEX_MODEL" ]; then
    AGENTS_COLORED="$AGENTS_COLORED ${CODEX_MODEL}"
    AGENTS_PLAIN="$AGENTS_PLAIN ${CODEX_MODEL}"
  fi
  if [ -n "$CODEX_EFFORT" ]; then
    AGENTS_COLORED="$AGENTS_COLORED ${EFFORTCOL}${CODEX_EFFORT}${DEF}"
    AGENTS_PLAIN="$AGENTS_PLAIN ${CODEX_EFFORT}"
  fi
  AGENTS_COLORED="$AGENTS_COLORED ${DIMCOL}(${CODEX_VER})${DEF}"
  AGENTS_PLAIN="$AGENTS_PLAIN (${CODEX_VER})"

  if [ ! -f "$READY_FILE" ]; then
    STATE_COLORED="${INIT}INIT ${SPINNER}"
    STATE_PLAIN="INIT X"
  elif [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
      STATE_COLORED="${DISC}DISCUSSING ${SPINNER}${DEF} (pid $PID)"
      STATE_PLAIN="DISCUSSING X (pid $PID)"
    else
      STATE_COLORED="${READY}READY"
      STATE_PLAIN="READY"
    fi
  else
    STATE_COLORED="${READY}READY"
    STATE_PLAIN="READY"
  fi

  if [ "${EDIT_OWNER:-idle}" = "idle" ]; then
    EDIT_COLORED="${DEF}idle"
    EDIT_PLAIN="idle"
  else
    EDIT_COLORED="${EDITCOL}${EDIT_OWNER}${DEF}"
    EDIT_PLAIN="$EDIT_OWNER"
  fi

  # Plain widths for padding math (escapes don't take visible cells)
  LEFT_PLAIN="  ${APP} ${VERSION}${AGENTS_PLAIN}"
  RIGHT_PLAIN="${NOW}  │  editing: ${EDIT_PLAIN}  │  status: ${STATE_PLAIN}  "

  # Use tmux's pane width — more reliable than tput inside a 1-row pane.
  COLS=$(tmux display-message -p -t "${TMUX_PANE:-}" '#{pane_width}' 2>/dev/null)
  [ -z "$COLS" ] && COLS=$(tput cols 2>/dev/null || echo 200)
  LL=${#LEFT_PLAIN}
  RL=${#RIGHT_PLAIN}
  PAD=$(( COLS - LL - RL ))
  [ $PAD -lt 1 ] && PAD=1

  printf '\r%s\033[2K' "$BG"
  printf '  %s%s%s %s%s%s' "$TITLE" "$APP" "$DEF" "$VER" "$VERSION" "$DEF"
  printf '%s' "$AGENTS_COLORED"
  printf '%*s' "$PAD" ""
  printf '%s%s%s  %s│%s  ' "$TIMECOL" "$NOW" "$DEF" "$SEP" "$DEF"
  printf 'editing: %s  %s│%s  ' "$EDIT_COLORED" "$SEP" "$DEF"
  printf 'status: %s  ' "$STATE_COLORED"
  printf '\033[K%s' "$RESET"

  sleep 0.15
done
