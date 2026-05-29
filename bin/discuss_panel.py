#!/usr/bin/env python3
"""Live discussion panel for the parley discuss strip.

Runs in the discuss pane in place of the old plain `tail -f`. Redraws an
animated header — elapsed clock, turn count, per-agent turn tallies with a
spinner on whoever is currently thinking — above the rolling orchestrator log:

    DISCUSS · should we cache the user lookups?              04:12
    ⠹ waiting for codex     turn 6     claude 3   codex 3
    ──────────────────────────────────────────────────────────────
    [12:03:01] turn 5: claude replied — ...
    [12:03:14] turn 6: waiting for codex

Elapsed time, turn tallies, who we're waiting on, and the phase come from the
JSON state file discuss.py writes; the body is the tail of the dialog log. The
clock and spinner are animated here, independent of the orchestrator, so the
panel stays alive even while an agent is mid-thought (the orchestrator is
blocked waiting on it and isn't updating anything).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from parley_paths import dialog_log_path, dialog_pid_path, dialog_state_path

STATE_PATH = dialog_state_path()
LOG_PATH = dialog_log_path()
PID_PATH = dialog_pid_path()

SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
FRAME_SECS = 0.12

# 256-color palette (matches bin/status.sh).
RESET = "\033[0m"
PINK = "\033[1;38;5;213m"
CLAUDECOL = "\033[38;5;209m"   # salmon
CODEXCOL = "\033[38;5;79m"     # teal
TIMECOL = "\033[38;5;221m"     # amber
DIM = "\033[38;5;244m"
DEF = "\033[38;5;252m"
GREEN = "\033[1;38;5;120m"
SPINCOL = "\033[1;38;5;213m"   # pink spinner


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return {}


def orchestrator_alive() -> bool:
    try:
        pid = int(PID_PATH.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def tail_lines(n: int) -> list[str]:
    if n <= 0:
        return []
    try:
        with open(LOG_PATH, "r", errors="replace") as f:
            lines = f.read().splitlines()
    except (FileNotFoundError, OSError):
        return []
    return lines[-n:]


def fmt_elapsed(secs: float) -> str:
    """Always mm:ss; for runs over an hour, minutes simply count past 60."""
    secs = int(max(0, secs))
    m, s = divmod(secs, 60)
    return f"{m:02d}:{s:02d}"


def trunc(s: str, width: int) -> str:
    """Truncate to `width` visible columns (plain text — the body has no ANSI)."""
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    return s[: max(0, width - 1)] + "…"


def term_size() -> tuple[int, int]:
    try:
        ts = os.get_terminal_size(sys.stdout.fileno())
        return ts.columns, ts.lines
    except OSError:
        return 80, 10


def render(frame: int) -> str:
    cols, rows = term_size()
    state = read_state()
    spin = SPIN[frame % len(SPIN)]

    phase = state.get("phase", "")
    ended = phase == "ended"
    # If the orchestrator vanished without marking itself ended (crash), don't
    # animate "waiting" forever — show it as ended.
    if state and not ended and not orchestrator_alive():
        ended = True
        state.setdefault("ended_reason", "stopped")

    started = state.get("started_at")
    ended_at = state.get("ended_at")
    if started:
        end_ref = ended_at if (ended and ended_at) else time.time()
        elapsed = fmt_elapsed(end_ref - started)
    else:
        elapsed = "00:00"

    turn = state.get("turn", 0)
    waiting = state.get("waiting_for")
    waiting_since = state.get("waiting_since")

    # Per-turn elapsed (only meaningful while waiting on someone).
    if waiting and waiting_since and not ended:
        wait_str = fmt_elapsed(time.time() - waiting_since)
    else:
        wait_str = None

    # --- single header line: [mm:ss · turn N] · <status> ---
    if ended:
        reason = state.get("ended_reason")
        if reason == "complete":
            status_txt = f"{GREEN}✓ agreed{DEF}"
            status_plain = "✓ agreed"
        elif reason == "stopped":
            status_txt = f"{DIM}■ stopped{DEF}"
            status_plain = "■ stopped"
        else:
            status_txt = f"{DIM}■ ended{DEF}"
            status_plain = "■ ended"
    elif not started:
        status_txt = f"{SPINCOL}{spin}{DEF} starting…"
        status_plain = f"{spin} starting…"
    elif waiting:
        wcol = CLAUDECOL if waiting == "claude" else CODEXCOL
        parens = f" ({wait_str})" if wait_str else ""
        status_txt = (f"waiting for {wcol}{waiting}{DEF} {SPINCOL}{spin}{DEF}"
                      f"{DIM}{parens}{DEF}")
        status_plain = f"waiting for {waiting} {spin}{parens}"
    else:
        status_txt = f"{SPINCOL}{spin}{DEF} thinking…"
        status_plain = f"{spin} thinking…"

    meta_txt = f"{DIM}[{DEF}{TIMECOL}{elapsed}{DEF}{DIM} · turn {turn}]{DEF}"
    line1 = f"{meta_txt} {DIM}·{DEF} {status_txt}"
    # The existing "\033[K" after line1 in the output stream clears any trailing
    # remnants from a previous (longer) frame, so no manual padding needed.

    sep = f"{DIM}{'─' * cols}{DEF}"

    body = tail_lines(rows - 2)

    # Cursor home, then clear-to-EOL per line (no full clear → no flicker).
    out = ["\033[H", line1, "\033[K"]
    out += ["\r\n", sep, "\033[K"]
    for ln in body:
        out += ["\r\n", DEF, trunc(ln, cols), RESET, "\033[K"]
    out.append("\033[J")  # wipe any rows left over from a taller previous frame
    return "".join(out)


def main() -> None:
    sys.stdout.write("\033[?25l")  # hide cursor
    sys.stdout.flush()
    frame = 0
    try:
        while True:
            sys.stdout.write(render(frame))
            sys.stdout.flush()
            frame += 1
            time.sleep(FRAME_SECS)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h" + RESET + "\n")  # restore cursor
        sys.stdout.flush()


if __name__ == "__main__":
    main()
