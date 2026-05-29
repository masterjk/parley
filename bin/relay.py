#!/usr/bin/env python3
"""Relay: route user input to Claude and Codex tmux panes.

Addressing:
  @claude <msg>  -> Claude only
  @codex <msg>   -> Codex only
  <msg>          -> both panes (plain message, no prefix)

`@` triggers an autocomplete menu. A bare `@` (no target chosen) is rejected.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.styles import Style
from prompt_toolkit.validation import ValidationError, Validator

from parley_paths import (
    agents_ready_path,
    dialog_log_path,
    dialog_pane_path,
    dialog_pid_path,
    ensure_runtime_dirs,
    transcript_path,
)

CLAUDE_PANE = os.environ.get("CLAUDE_PANE", "")
CODEX_PANE = os.environ.get("CODEX_PANE", "")
BIN_DIR = Path(__file__).resolve().parent
WORK_DIR = os.environ.get("WORK_DIR", str(BIN_DIR.parent))

ensure_runtime_dirs()
TRANSCRIPT = str(transcript_path())
DIALOG_SCRIPT = str(BIN_DIR / "discuss.py")
EDIT_OWNER_SCRIPT = str(BIN_DIR / "edit_owner.py")
DIALOG_PID_FILE = str(dialog_pid_path())
DIALOG_PANE_FILE = str(dialog_pane_path())
DIALOG_LOG = str(dialog_log_path())
DIALOG_PANE_HEIGHT = 10  # lines for the inline dialog strip above the relay
READY_FILE = str(agents_ready_path())

TARGETS = ["@claude", "@codex"]
# Slash commands offered in tab/typing autocomplete. Ordered roughly by
# how often Captain reaches for each.
COMMANDS = [
    "/discuss",
    "/status",
    "/quit",
]
COMPLETION_MENU_ROWS = max(len(TARGETS), len(COMMANDS)) + 1

STYLE = Style.from_dict({
    "prompt": "ansicyan bold",
    "completion-menu.completion": "bg:#333333 #ffffff",
    "completion-menu.completion.current": "bg:#0066cc #ffffff bold",
})


class RelayCompleter(Completer):
    """Autocomplete @targets and /commands. Triggers on the first char of
    either prefix; Tab cycles the menu (prompt_toolkit default)."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if " " in text:
            return
        if text.startswith("@"):
            yield from self._yield_matches(text, TARGETS)
        elif text.startswith("/"):
            yield from self._yield_matches(text, COMMANDS)

    def _yield_matches(self, text, options):
        prefix = text.lower()
        for opt in options:
            option = opt + " "
            if option.startswith(prefix):
                yield Completion(option[len(text):], display=opt)


class AtValidator(Validator):
    """Reject input that starts with `@` but has no valid target."""

    def validate(self, document):
        text = document.text.strip()
        if not text.startswith("@"):
            return
        head = text.split(" ", 1)[0].lower()
        if head not in TARGETS:
            raise ValidationError(
                message=f"Unknown target '{head}'. Use {', '.join(TARGETS)}.",
                cursor_position=len(text),
            )
        # Must have a message after the target
        if " " not in text or not text.split(" ", 1)[1].strip():
            raise ValidationError(
                message=f"Missing message after {head}.",
                cursor_position=len(text),
            )


def send_to_pane(pane: str, msg: str) -> None:
    subprocess.run(["tmux", "set-buffer", "--", msg], check=True)
    subprocess.run(["tmux", "paste-buffer", "-t", pane], check=True)
    time.sleep(0.3)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_jsonl(path: str, entry: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def log_message(msg: str, recipients: list[str], kind: str = "message") -> None:
    entry = {
        "ts": utc_ts(),
        "author": "user",
        "kind": kind,
        "recipients": recipients,
        "content": msg,
    }
    write_jsonl(TRANSCRIPT, entry)


def recipient_label(recipients: list[str] | str) -> str:
    if isinstance(recipients, str):
        return recipients
    if recipients == ["claude", "codex"]:
        return "all"
    return ", ".join(str(r) for r in recipients)


def run_python_tool(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", *args],
        cwd=WORK_DIR,
        text=True,
        capture_output=True,
    )


def print_tool_result(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)


def dialog_pid() -> int | None:
    try:
        pid = int(Path(DIALOG_PID_FILE).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    try:
        os.kill(pid, 0)  # alive?
        return pid
    except ProcessLookupError:
        return None


def kill_dialog_pane() -> None:
    """Kill the dialog strip pane if one is recorded."""
    try:
        pane = Path(DIALOG_PANE_FILE).read_text().strip()
    except FileNotFoundError:
        return
    if pane:
        subprocess.run(["tmux", "kill-pane", "-t", pane],
                       stderr=subprocess.DEVNULL)
    Path(DIALOG_PANE_FILE).unlink(missing_ok=True)


def cmd_discuss(topic: str) -> None:
    if dialog_pid() is not None:
        print("A discussion is already running. Use /discuss off first.")
        return
    if not topic.strip():
        print("Usage: /discuss <topic>")
        return

    # Seed the log with a pink banner so the strip shows something obvious
    # the instant `tail -f` lights up.
    bar = "═" * 78
    banner = (
        f"\033[1;38;5;213m{bar}\033[0m\n"
        f"\033[1;38;5;213m  ▶  DISCUSSION  →  {topic}\033[0m\n"
        f"\033[1;38;5;213m{bar}\033[0m\n"
    )
    with open(DIALOG_LOG, "w") as f:
        f.write(banner)

    # Remove a leftover dialog pane from a prior discussion, if any.
    kill_dialog_pane()

    # Identify our own (relay) pane.
    relay_pane = subprocess.check_output(
        ["tmux", "display-message", "-p", "#{pane_id}"], text=True
    ).strip()

    # Make room for the discuss strip without shrinking captain. tmux's
    # split-window takes its new pane's space FROM the pane being split, so a
    # naive split-above-relay would steal from captain. Instead, pre-shrink
    # the claude/codex row by H + 1 lines (H for the discuss pane + 1 for the
    # new inter-pane border tmux inserts). The freed lines flow down to the
    # relay (next sibling in the root vertical layout). The subsequent split
    # then takes its space from relay's new slack, leaving captain at its
    # original height.
    try:
        claude_h = int(subprocess.check_output(
            ["tmux", "display-message", "-t", CLAUDE_PANE, "-p", "#{pane_height}"],
            text=True,
        ).strip())
        # Floor at 5 lines so we never collapse the top section entirely on
        # very small terminals.
        new_claude_h = max(5, claude_h - DIALOG_PANE_HEIGHT - 1)
        subprocess.run(
            ["tmux", "resize-pane", "-t", CLAUDE_PANE, "-y", str(new_claude_h)],
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, ValueError):
        pass  # fall through to the split; layout may be slightly off but won't break

    new_pane = subprocess.check_output([
        "tmux", "split-window",
        "-d",                 # don't move focus to the new pane
        "-b", "-v",           # before + vertical → above the target
        "-l", str(DIALOG_PANE_HEIGHT),
        "-t", relay_pane,
        "-P", "-F", "#{pane_id}",
        f"tail -f {DIALOG_LOG}",
    ], text=True).strip()
    Path(DIALOG_PANE_FILE).write_text(new_pane)

    # Pink border around the dialog pane — visually pops in.
    subprocess.run(
        ["tmux", "select-pane", "-t", new_pane, "-P", "fg=colour213"],
        stderr=subprocess.DEVNULL,
    )
    # Border label. @parley_label is the user option that pane-border-format
    # reads (see start.sh).
    subprocess.run(
        ["tmux", "set-option", "-p", "-t", new_pane, "@parley_label", "[DISCUSS]"],
        stderr=subprocess.DEVNULL,
    )

    env = {
        **os.environ,
        "CLAUDE_PANE": CLAUDE_PANE,
        "CODEX_PANE": CODEX_PANE,
        "WORK_DIR": WORK_DIR,
    }
    log_f = open(DIALOG_LOG, "a")
    proc = subprocess.Popen(
        ["python3", DIALOG_SCRIPT, topic],
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"Discussion started (pid {proc.pid}). Topic: {topic}")
    print(f"  Stop and close strip:  /discuss off")


def cmd_quit() -> None:
    """Stop any dialog and kill the whole tmux session."""
    pid = dialog_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        Path(DIALOG_PID_FILE).unlink(missing_ok=True)
    try:
        session = subprocess.check_output(
            ["tmux", "display-message", "-p", "#{session_name}"], text=True
        ).strip()
        subprocess.run(["tmux", "kill-session", "-t", session])
    except Exception as e:
        print(f"Failed to kill session: {e}")


def cmd_discuss_off() -> None:
    """Stop any active discussion AND close the strip pane."""
    pid = dialog_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        Path(DIALOG_PID_FILE).unlink(missing_ok=True)
    kill_dialog_pane()
    print("Discussion closed.")


def cmd_status() -> None:
    print("Relay status:")
    edit = run_python_tool([EDIT_OWNER_SCRIPT, "status"])
    if edit.returncode == 0 and edit.stdout.strip():
        print(f"  {edit.stdout.strip()}")
    elif edit.stderr.strip():
        print(f"  editing: unknown ({edit.stderr.strip()})")

    latest = latest_transcript_entry()
    if latest:
        print(f"  last route: {recipient_label(latest.get('recipients', []))} at {latest.get('ts', '?')}")
    else:
        print("  last route: none")

    pid = dialog_pid()
    if pid is None:
        print("  discussion: not running")
        return
    print(f"  discussion: running (pid {pid}). Last log lines:")
    try:
        with open(DIALOG_LOG) as f:
            tail = f.readlines()[-10:]
            print("".join(tail))
    except FileNotFoundError:
        print("(no log yet)")


def latest_transcript_entry() -> dict | None:
    try:
        with open(TRANSCRIPT) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            return entry
    return None


def send_to_recipients(recipients: list[str], msg: str) -> None:
    if "claude" in recipients:
        send_to_pane(CLAUDE_PANE, msg)
    if "codex" in recipients:
        send_to_pane(CODEX_PANE, msg)


def route(line: str) -> None:
    head = line.split(" ", 1)[0].lower() if line.startswith("@") else None

    if head == "@claude":
        body = line.split(" ", 1)[1]
        recipients = ["claude"]
    elif head == "@codex":
        body = line.split(" ", 1)[1]
        recipients = ["codex"]
    else:
        # No @ prefix → broadcast to both agents.
        body = line
        recipients = ["claude", "codex"]

    send_to_recipients(recipients, body)
    log_message(body, recipients)


def wait_for_ready(timeout: float = 60.0) -> None:
    """Block until the start.sh subshell signals agents are ready (intros sent)."""
    if os.path.exists(READY_FILE):
        return
    print("Initializing agents... (waiting for prompts to land)")
    spinner = "|/-\\"
    i = 0
    start = time.time()
    while not os.path.exists(READY_FILE):
        if time.time() - start > timeout:
            print(f"\rTimed out after {int(timeout)}s — opening relay anyway.")
            return
        sys.stdout.write(f"\r  {spinner[i % len(spinner)]} ")
        sys.stdout.flush()
        i += 1
        time.sleep(0.15)
    print("\r  ready.                          ")


def print_startup_banner() -> None:
    """Short banner shown once on launch — the day-to-day essentials only."""
    print(f"\nAgent relay ready.")
    print(f"  @claude / @codex for a specific agent; unprefixed → both. Tab cycles.")
    print(f"  /discuss <topic>       start an agent-to-agent discussion")
    print(f"  /quit                  kill the tmux session\n")


def main() -> None:
    if not CLAUDE_PANE or not CODEX_PANE:
        print("CLAUDE_PANE and CODEX_PANE must be set.", file=sys.stderr)
        sys.exit(1)

    wait_for_ready()

    print_startup_banner()

    # Reserve enough rows for the longer of the two completion menus
    # (@targets, /commands). With only one reserved row, prompt_toolkit
    # clips the popup when the relay prompt is at the bottom of its pane.
    session = PromptSession(
        completer=RelayCompleter(),
        complete_while_typing=True,
        validator=AtValidator(),
        validate_while_typing=False,
        style=STYLE,
        reserve_space_for_menu=COMPLETION_MENU_ROWS,
    )

    while True:
        try:
            line = session.prompt("captain> ")
        except (EOFError, KeyboardInterrupt):
            print("\nExiting relay.")
            break

        line = line.strip()
        if not line:
            continue
        command, _, arg = line.partition(" ")
        command = command.lower()
        arg = arg.strip()
        if line == "/quit":
            cmd_quit()
            break
        if command == "/discuss":
            if arg.lower() == "off":
                cmd_discuss_off()
            else:
                cmd_discuss(arg)
            continue
        if command == "/status":
            cmd_status()
            continue

        route(line)


if __name__ == "__main__":
    main()
