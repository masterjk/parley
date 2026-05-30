#!/usr/bin/env python3
"""Runtime paths for parley.

Single source of truth for where parley reads and writes mutable state. Both
Python tools (relay, discuss, peek, edit_owner, doctor) and shell tools
(start.sh, status.sh) get paths from here:

  Python:  from parley_paths import state_dir, log_dir, ...
  Shell:   eval "$(python3 path/to/parley_paths.py --shell-export)"

Conventions:
  * Read-only assets (prompts, scripts) live next to the code in bin/. In a
    Homebrew install that's libexec/bin/; in a source checkout it's
    repo/bin/. Either way, callers locate them relative to __file__.
  * Mutable state never lives inside the source tree. On Linux it goes under
    XDG dirs; on macOS, logs go under ~/Library/Logs/parley to match
    platform convention.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _xdg(env_var: str, default_subpath: str) -> Path:
    base = os.environ.get(env_var)
    if base:
        return Path(base)
    return Path.home() / default_subpath


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "parley"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "parley"


def cursors_dir() -> Path:
    return state_dir() / "cursors"


def log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "parley"
    return state_dir() / "logs"


def transcript_path() -> Path:
    return state_dir() / "transcript.jsonl"


def edit_owner_path() -> Path:
    return state_dir() / "edit_owner.json"


def dialog_result_path() -> Path:
    return state_dir() / "dialog.result"


def dialog_state_path() -> Path:
    return state_dir() / "dialog.state.json"


def dialog_log_path() -> Path:
    return log_dir() / "dialog.log"


def startup_log_path() -> Path:
    return log_dir() / "startup.log"


def dialog_pid_path() -> Path:
    return cursors_dir() / "dialog.pid"


def dialog_pane_path() -> Path:
    return cursors_dir() / "dialog.pane"


def agents_ready_path() -> Path:
    return cursors_dir() / "agents_ready"


def ensure_runtime_dirs() -> None:
    for d in (config_dir(), state_dir(), cursors_dir(), log_dir()):
        d.mkdir(parents=True, exist_ok=True)


def latest_session_for(agent: str, work_dir: str | None = None) -> Path | None:
    """Return the agent's most-recently-modified session JSONL.

    Agents — codex in particular — silently rotate their session file mid-run,
    so the startup-pinned cursor (cursors/<agent>.session) goes stale and
    callers polling it see no new content. This helper looks up the current
    file dynamically.

    Falls back to the startup pin if no candidates are found.
    """
    candidates: list[Path] = []
    if agent == "claude":
        if not work_dir:
            return None
        proj = Path.home() / ".claude" / "projects" / str(work_dir).replace("/", "-")
        if proj.is_dir():
            candidates = list(proj.glob("*.jsonl"))
    elif agent == "codex":
        from datetime import date, timedelta
        sess = Path.home() / ".codex" / "sessions"
        # rglob across all dates is slow once history accumulates; limit to
        # today and the prior two days — a parley session never spans more.
        today = date.today()
        for delta in range(3):
            d = today - timedelta(days=delta)
            day_dir = sess / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
            if day_dir.is_dir():
                candidates.extend(day_dir.glob("rollout-*.jsonl"))

    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)

    # Fallback: read the startup pin written by start.sh's Pipeline B.
    pinned = cursors_dir() / f"{agent}.session"
    if pinned.exists():
        path = Path(pinned.read_text().strip())
        if path.exists():
            return path
    return None


_SHELL_EXPORTS = {
    "PARLEY_CONFIG_DIR": config_dir,
    "PARLEY_STATE_DIR": state_dir,
    "PARLEY_CURSORS_DIR": cursors_dir,
    "PARLEY_LOG_DIR": log_dir,
    "PARLEY_TRANSCRIPT": transcript_path,
    "PARLEY_EDIT_OWNER": edit_owner_path,
    "PARLEY_DIALOG_RESULT": dialog_result_path,
    "PARLEY_DIALOG_STATE": dialog_state_path,
    "PARLEY_DIALOG_LOG": dialog_log_path,
    "PARLEY_STARTUP_LOG": startup_log_path,
    "PARLEY_DIALOG_PID": dialog_pid_path,
    "PARLEY_DIALOG_PANE": dialog_pane_path,
    "PARLEY_AGENTS_READY": agents_ready_path,
}


def shell_export() -> str:
    lines = []
    for name, getter in _SHELL_EXPORTS.items():
        value = str(getter())
        # Single-quote and escape any embedded quotes so paths with spaces
        # round-trip safely through `eval`.
        escaped = value.replace("'", "'\\''")
        lines.append(f"export {name}='{escaped}'")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "--shell-export":
        print("usage: parley_paths.py --shell-export", file=sys.stderr)
        return 2
    ensure_runtime_dirs()
    print(shell_export())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
