#!/usr/bin/env python3
"""Read the other agent's recent conversation with Captain.

Default: returns messages newer than the last time YOU peeked at this agent
(incremental cursor stored in the parley runtime cursor directory).

  bin/peek.py claude               # new since last peek
  bin/peek.py codex --tail 20      # last 20 messages, ignore cursor
  bin/peek.py claude --all         # full session, ignore cursor
  bin/peek.py codex --since 2026-05-28T14:30:00Z
  bin/peek.py claude --no-update   # show new but don't advance cursor
  bin/peek.py claude --reader captain

The "current session" file is discovered dynamically at each invocation —
agents may rotate their session file mid-run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from parley_paths import cursors_dir, latest_session_for

WORK_DIR = Path(os.environ.get("WORK_DIR", Path(__file__).resolve().parent.parent))
CURSORS_DIR = cursors_dir()


def session_path(agent: str) -> Path:
    """Resolve to the agent's CURRENT session JSONL.

    Discovered dynamically — agents (codex especially) rotate their session
    file silently mid-run, so we can't trust the startup pin alone.
    """
    path = latest_session_for(agent, str(WORK_DIR))
    if not path:
        sys.exit(f"No session file found for {agent}. Did start.sh run?")
    return path


def parse_claude_line(o: dict):
    """Return (ts, role, text) or None to skip."""
    t = o.get("type")
    if t not in ("user", "assistant"):
        return None
    ts = o.get("timestamp")
    content = o.get("message", {}).get("content")
    if isinstance(content, str):
        return (ts, "captain", content)
    if isinstance(content, list):
        texts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "text":
                texts.append(item.get("text", ""))
            elif kind == "tool_use":
                name = item.get("name", "?")
                texts.append(f"[ran tool: {name}]")
        if not texts:
            return None
        role = "captain" if t == "user" else "claude"
        return (ts, role, "\n".join(texts))
    return None


def parse_codex_line(o: dict):
    if o.get("type") != "response_item":
        return None
    payload = o.get("payload", {})
    if payload.get("type") != "message":
        return None
    role_raw = payload.get("role")
    if role_raw not in ("user", "assistant"):
        return None
    ts = o.get("timestamp")
    texts = []
    for item in payload.get("content", []):
        if isinstance(item, dict) and item.get("text"):
            texts.append(item["text"])
    if not texts:
        return None
    role = "captain" if role_raw == "user" else "codex"
    return (ts, role, "\n".join(texts))


PARSERS = {"claude": parse_claude_line, "codex": parse_codex_line}


def cursor_path(agent: str, reader: str | None = None) -> Path:
    if reader:
        safe_reader = "".join(ch for ch in reader if ch.isalnum() or ch in ("-", "_"))
        if not safe_reader:
            safe_reader = "reader"
        return CURSORS_DIR / f"{safe_reader}_{agent}_read.txt"
    return CURSORS_DIR / f"{agent}_read.txt"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("agent", choices=["claude", "codex"])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--tail", type=int, help="last N messages, ignore cursor")
    g.add_argument("--all", action="store_true", help="full session")
    g.add_argument("--since", help="ISO ts; only messages newer")
    ap.add_argument(
        "--reader",
        help="use a reader-specific cursor instead of the default agent cursor",
    )
    ap.add_argument("--no-update", action="store_true", help="don't advance cursor")
    args = ap.parse_args()

    CURSORS_DIR.mkdir(parents=True, exist_ok=True)

    parse = PARSERS[args.agent]
    msgs = []
    with open(session_path(args.agent)) as f:
        for line in f:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = parse(o)
            if m:
                msgs.append(m)

    advance_cursor = False
    if args.tail:
        msgs = msgs[-args.tail:]
    elif args.all:
        pass
    elif args.since:
        msgs = [m for m in msgs if m[0] > args.since]
    else:
        cur = cursor_path(args.agent, args.reader)
        last = cur.read_text().strip() if cur.exists() else ""
        msgs = [m for m in msgs if m[0] > last]
        advance_cursor = not args.no_update

    if not msgs:
        print(f"(no new messages from {args.agent})")
        return

    for ts, role, text in msgs:
        print(f"[{ts} {role}]")
        print(text)
        print()

    if advance_cursor:
        cursor_path(args.agent).write_text(msgs[-1][0])


if __name__ == "__main__":
    main()
