#!/usr/bin/env python3
"""Manage the shared edit-owner heartbeat.

This is intentionally file-backed so the relay, status bar, Claude, and Codex
can coordinate without a daemon.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from parley_paths import edit_owner_path, ensure_runtime_dirs

OWNERS = {"captain", "claude", "codex"}
DEFAULT_TTL_SECONDS = 300


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ttl_seconds() -> int:
    raw = os.environ.get("PARLEY_EDIT_OWNER_TTL", "")
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def read_state() -> dict:
    path = edit_owner_path()
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(state: dict) -> None:
    ensure_runtime_dirs()
    edit_owner_path().write_text(json.dumps(state, sort_keys=True) + "\n")


def idle_state(previous: dict | None = None) -> dict:
    state = {
        "owner": "idle",
        "note": "",
        "updated_at": now_iso(),
        "updated_epoch": time.time(),
    }
    if previous and previous.get("owner") not in ("", None, "idle"):
        state["previous_owner"] = previous.get("owner")
    return state


def effective_state() -> dict:
    raw = read_state()
    owner = str(raw.get("owner") or "idle").lower()
    updated_epoch = float(raw.get("updated_epoch") or 0)
    age = max(0, int(time.time() - updated_epoch)) if updated_epoch else 0

    state = {
        "owner": owner if owner in OWNERS else "idle",
        "note": str(raw.get("note") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
        "updated_epoch": updated_epoch,
        "age_seconds": age,
        "stale": False,
        "stale_owner": "",
    }
    if state["owner"] != "idle" and age > ttl_seconds():
        state["stale"] = True
        state["stale_owner"] = state["owner"]
        state["owner"] = "idle"
    return state


def set_owner(owner: str, note: str) -> dict:
    owner = owner.lower()
    if owner not in OWNERS:
        raise ValueError(f"owner must be one of: {', '.join(sorted(OWNERS))}")
    state = {
        "owner": owner,
        "note": note,
        "updated_at": now_iso(),
        "updated_epoch": time.time(),
    }
    write_state(state)
    return effective_state()


def release(owner: str | None, force: bool) -> tuple[bool, str]:
    raw = read_state()
    current = effective_state()
    if owner and not force and current["owner"] not in ("idle", owner):
        return False, f"not releasing; current edit owner is {current['owner']}"
    write_state(idle_state(raw))
    return True, "editing: idle"


def shell_value(value: object) -> str:
    return shlex.quote(str(value))


def print_shell(state: dict) -> None:
    values = {
        "EDIT_OWNER": state["owner"],
        "EDIT_NOTE": state["note"],
        "EDIT_AGE_SECONDS": state["age_seconds"],
        "EDIT_STALE": "1" if state["stale"] else "0",
        "EDIT_STALE_OWNER": state["stale_owner"],
    }
    for name, value in values.items():
        print(f"{name}={shell_value(value)}")


def print_human(state: dict) -> None:
    owner = state["owner"]
    if owner == "idle":
        if state["stale_owner"]:
            print(
                f"editing: idle ({state['stale_owner']} heartbeat stale, "
                f"{state['age_seconds']}s old)"
            )
        else:
            print("editing: idle")
        return

    note = f" - {state['note']}" if state["note"] else ""
    print(f"editing: {owner} ({state['age_seconds']}s ago){note}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status", help="print the current edit owner")
    status.add_argument("--shell", action="store_true", help="emit shell assignments")
    status.add_argument("--json", action="store_true", help="emit JSON")

    for name in ("claim", "heartbeat"):
        p = sub.add_parser(name, help=f"{name} edit ownership")
        p.add_argument("owner", choices=sorted(OWNERS))
        p.add_argument("note", nargs="*", help="optional context")

    release_p = sub.add_parser("release", help="release edit ownership")
    release_p.add_argument("owner", nargs="?", choices=sorted(OWNERS))
    release_p.add_argument("--force", action="store_true")

    args = parser.parse_args()

    try:
        if args.cmd == "status":
            state = effective_state()
            if args.json:
                print(json.dumps(state, sort_keys=True))
            elif args.shell:
                print_shell(state)
            else:
                print_human(state)
            return 0

        if args.cmd in ("claim", "heartbeat"):
            state = set_owner(args.owner, " ".join(args.note).strip())
            print_human(state)
            return 0

        if args.cmd == "release":
            ok, msg = release(args.owner, args.force)
            print(msg)
            return 0 if ok else 1
    except BrokenPipeError:
        return 0
    except Exception as exc:
        print(f"edit_owner: {exc}", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
