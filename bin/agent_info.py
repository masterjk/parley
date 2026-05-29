#!/usr/bin/env python3
"""Emit live model/effort/mode for each agent, as shell-evalable assignments.

The status bar (bin/status.sh) sources this on a throttle to show what each
agent is actually running. Values are read from the pinned session JSONLs whose
paths live in $PARLEY_CURSORS_DIR/{claude,codex}.session (written by start.sh),
and (for Claude's effortLevel) from settings.json:

  - Claude model:  the most recent `"model"` recorded in its Claude Code JSONL.
  - Claude effort: `effortLevel` from .claude/settings.json (project → global
                   precedence). Not in the JSONL; only present when the user
                   has explicitly set it, so callers omit when blank.
  - Claude mode:   most recent `{"type":"mode","mode":...}` record in the JSONL
                   (the `/fast` toggle, Opus-only). We only surface non-default
                   values; status.sh hides `normal` (the default).
  - Codex:         the most recent `turn_context` event's model + reasoning_effort.

CLI *versions* are intentionally NOT computed here — they never change during a
session, so status.sh snapshots them once via `claude/codex --version` rather
than paying a subprocess on every refresh.

Output (any missing value is an empty string; callers tolerate blanks):

    CLAUDE_MODEL='opus-4-7'
    CLAUDE_EFFORT='xhigh'
    CLAUDE_MODE='fast'
    CODEX_MODEL='gpt-5.5'
    CODEX_EFFORT='med'
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

EFFORT_ABBR = {"minimal": "min", "low": "low", "medium": "med", "high": "high"}


def _session_path(name: str) -> Path | None:
    cursors = os.environ.get("PARLEY_CURSORS_DIR")
    if not cursors:
        return None
    p = Path(cursors) / f"{name}.session"
    try:
        target = p.read_text().strip()
    except OSError:
        return None
    return Path(target) if target else None


def _shorten_claude_model(model: str) -> str:
    # claude-opus-4-7  ->  opus-4-7 ; drop any trailing -YYYYMMDD date stamp.
    model = re.sub(r"^claude-", "", model)
    model = re.sub(r"-\d{8}$", "", model)
    return model


def claude_model() -> str:
    path = _session_path("claude")
    if not path or not path.exists():
        return ""
    found = ""
    try:
        with path.open() as f:
            for line in f:
                m = re.findall(r'"model"\s*:\s*"([^"]+)"', line)
                if m:
                    found = m[-1]
    except OSError:
        return ""
    return _shorten_claude_model(found) if found else ""


def claude_mode() -> str:
    """Latest /fast toggle state from the JSONL ('normal' | 'fast' | '')."""
    path = _session_path("claude")
    if not path or not path.exists():
        return ""
    found = ""
    try:
        with path.open() as f:
            for line in f:
                # cheap prefilter to skip the 99% of lines that aren't mode records
                if '"type":"mode"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "mode":
                    found = d.get("mode", "") or found
    except OSError:
        return ""
    return found


def claude_effort() -> str:
    """`effortLevel` from project then global settings.json. Blank if unset."""
    cwd = Path(os.environ.get("WORK_DIR", "")).resolve() if os.environ.get("WORK_DIR") else Path.cwd()
    candidates = [
        cwd / ".claude" / "settings.local.json",
        cwd / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.json",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("effortLevel"):
            return str(data["effortLevel"])
    return ""


def codex_model_effort() -> tuple[str, str]:
    path = _session_path("codex")
    if not path or not path.exists():
        return "", ""
    model = ""
    effort = ""
    try:
        with path.open() as f:
            for line in f:
                if '"turn_context"' not in line:
                    continue
                try:
                    payload = json.loads(line).get("payload", {})
                except (json.JSONDecodeError, AttributeError):
                    continue
                model = payload.get("model") or model
                eff = payload.get("reasoning_effort")
                if eff is None:
                    settings = (payload.get("collaboration_mode") or {}).get("settings") or {}
                    eff = settings.get("reasoning_effort")
                if eff:
                    effort = EFFORT_ABBR.get(eff, eff)
    except OSError:
        return "", ""
    return model, effort


def _sh(name: str, value: str) -> str:
    return f"{name}='{value}'"


def main() -> None:
    c_model = claude_model()
    c_effort = claude_effort()
    c_mode = claude_mode()
    x_model, x_effort = codex_model_effort()
    print(_sh("CLAUDE_MODEL", c_model))
    print(_sh("CLAUDE_EFFORT", c_effort))
    print(_sh("CLAUDE_MODE", c_mode))
    print(_sh("CODEX_MODEL", x_model))
    print(_sh("CODEX_EFFORT", x_effort))


if __name__ == "__main__":
    main()
