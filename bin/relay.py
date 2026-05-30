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
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from prompt_toolkit.validation import ValidationError, Validator

import parley_theme
from parley_paths import (
    agents_ready_path,
    dialog_log_path,
    dialog_pane_path,
    dialog_pid_path,
    dialog_state_path,
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
DIALOG_STATE_FILE = str(dialog_state_path())
DIALOG_LOG = str(dialog_log_path())
DIALOG_PANEL_SCRIPT = str(BIN_DIR / "discuss_panel.py")
DIALOG_PANE_HEIGHT = 10  # lines for the inline dialog strip above the relay
READY_FILE = str(agents_ready_path())

TARGETS = ["@claude", "@codex"]
# Slash commands offered in tab/typing autocomplete. Ordered roughly by
# how often Captain reaches for each.
COMMANDS = [
    "/discuss",
    "/theme",
    "/status",
    "/quit",
]
COMPLETION_MENU_ROWS = max(len(TARGETS), len(COMMANDS), len(parley_theme.list_themes())) + 1


def make_style() -> Style:
    """Prompt style sourced from the active theme (its 'title' color)."""
    return Style.from_dict({
        "prompt": parley_theme.pt_color(parley_theme.resolve()["title"]),
        "completion-menu.completion": "bg:#333333 #ffffff",
        "completion-menu.completion.current": "bg:#0066cc #ffffff bold",
    })


class RelayCompleter(Completer):
    """Autocomplete @targets and /commands. Triggers on the first char of
    either prefix at the start of a line; Tab cycles the menu (prompt_toolkit
    default). Also offers @targets as the *first* argument after `/discuss`
    so `/discuss @cl<TAB>` completes to `/discuss @claude `.
    """

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # The "current token" is what we're typing — chars since the last space.
        token = text.rsplit(" ", 1)[-1]

        # Case 1: first token on the line (no space yet).
        if text == token:
            if token.startswith("@"):
                yield from self._yield_token_matches(token, TARGETS)
            elif token.startswith("/"):
                yield from self._yield_token_matches(token, COMMANDS)
            return

        # Case 2: `/discuss @…` — the starter-override token.
        # Only when this @-token is the FIRST argument after `/discuss`
        # (i.e. nothing non-blank between `/discuss ` and the token).
        if text.startswith("/discuss ") and token.startswith("@"):
            cut = -len(token) if token else None
            between = text[len("/discuss "):cut]
            if between.strip() == "":
                yield from self._yield_token_matches(token, TARGETS)
            return

        # Case 3: `/theme <name>` — complete theme names as the first argument.
        if text.startswith("/theme ") and text[len("/theme "):] == token:
            yield from self._yield_token_matches(token, parley_theme.list_themes())

    def _yield_token_matches(self, token, options):
        prefix = token.lower()
        for opt in options:
            if opt.lower().startswith(prefix):
                yield Completion(opt + " ", start_position=-len(token), display=opt)


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
        print("Usage: /discuss [@claude|@codex] <topic>")
        return

    # Seed the log (plain text — the panel renders its own colored header and
    # truncates body lines, so no ANSI in the body) and clear any stale state
    # from a previous discussion so the panel doesn't flash old tallies before
    # discuss.py publishes fresh ones.
    with open(DIALOG_LOG, "w") as f:
        f.write(f"Starting discussion: {topic}\n")
    Path(DIALOG_STATE_FILE).unlink(missing_ok=True)

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
        f"python3 '{DIALOG_PANEL_SCRIPT}'",
    ], text=True).strip()
    Path(DIALOG_PANE_FILE).write_text(new_pane)

    # Accent border around the dialog pane — visually pops in. Color follows
    # the active theme (harbor's accent is the original pink).
    accent = parley_theme.tmux_color(parley_theme.resolve()["accent"])
    subprocess.run(
        ["tmux", "select-pane", "-t", new_pane, "-P", f"fg={accent}"],
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
        ["python3", DIALOG_SCRIPT, *topic.split()],
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


def apply_theme_to_tmux() -> None:
    """Re-apply theme-dependent tmux styling after a live /theme switch: the
    pane-border label color, and the discuss strip border if one is open. The
    status bar and discuss panel reload their own colors on a throttle."""
    try:
        session_name = subprocess.check_output(
            ["tmux", "display-message", "-p", "#{session_name}"], text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        session_name = ""
    if session_name:
        theme = parley_theme.resolve()
        subprocess.run(
            ["tmux", "set-option", "-t", session_name, "pane-border-format",
             parley_theme.pane_border_format()],
            stderr=subprocess.DEVNULL,
        )
        # tmux's built-in bottom status line, to match the top strip.
        subprocess.run(
            ["tmux", "set-option", "-t", session_name, "status-style",
             f"bg={parley_theme.tmux_color(theme['bg'])},"
             f"fg={parley_theme.tmux_color(theme['text'])}"],
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["tmux", "set-option", "-t", session_name, "window-status-current-style",
             f"fg={parley_theme.tmux_color(theme['accent'])},bold"],
            stderr=subprocess.DEVNULL,
        )
    try:
        pane = Path(DIALOG_PANE_FILE).read_text().strip()
    except FileNotFoundError:
        pane = ""
    if pane:
        accent = parley_theme.tmux_color(parley_theme.resolve()["accent"])
        subprocess.run(
            ["tmux", "select-pane", "-t", pane, "-P", f"fg={accent}"],
            stderr=subprocess.DEVNULL,
        )


def cmd_theme(arg: str, session) -> None:
    """`/theme` lists themes + shows the active one; `/theme <name>` switches,
    persists the choice to the config, and re-applies live."""
    name = arg.strip().lower()
    if not name:
        cur = parley_theme.current_theme()
        print("Themes (* = active):")
        for t in parley_theme.list_themes():
            print(f"   {'*' if t == cur else ' '} {t}")
        print("Switch with: /theme <name>")
        return
    if not parley_theme.set_theme(name):
        print(f"Unknown theme '{name}'. "
              f"Available: {', '.join(parley_theme.list_themes())}")
        return
    apply_theme_to_tmux()
    if session is not None:
        session.style = make_style()  # prompt_toolkit reads session.style live
    print(f"Theme → {name}. Config saved; status bar and panels recolor within ~3s.")


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
    print(f"  /discuss [@claude|@codex] <topic>   start an agent-to-agent discussion")
    print(f"  /theme [name]          switch color theme (no name → list)")
    print(f"  /quit                  kill the tmux session\n")


def main() -> None:
    if not CLAUDE_PANE or not CODEX_PANE:
        print("CLAUDE_PANE and CODEX_PANE must be set.", file=sys.stderr)
        sys.exit(1)

    wait_for_ready()

    print_startup_banner()

    # Ctrl-C mirrors the claude/codex CLI convention:
    #   - non-empty buffer → clear the current line, keep the prompt
    #   - empty buffer      → raise KeyboardInterrupt → caught below → /quit
    kb = KeyBindings()

    @kb.add("c-c")
    def _ctrl_c(event):
        buf = event.current_buffer
        if buf.text:
            buf.reset()
        else:
            event.app.exit(exception=KeyboardInterrupt)

    # Reserve enough rows for the longer of the two completion menus
    # (@targets, /commands). With only one reserved row, prompt_toolkit
    # clips the popup when the relay prompt is at the bottom of its pane.
    session = PromptSession(
        completer=RelayCompleter(),
        complete_while_typing=True,
        validator=AtValidator(),
        validate_while_typing=False,
        style=make_style(),
        reserve_space_for_menu=COMPLETION_MENU_ROWS,
        key_bindings=kb,
    )

    while True:
        try:
            line = session.prompt("captain> ")
        except KeyboardInterrupt:
            # Ctrl-C escalates to /quit (kills the tmux session, same as typing it).
            print("\n^C — /quit")
            cmd_quit()
            break
        except EOFError:
            # Ctrl-D just exits relay without tearing down tmux.
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
        if command == "/theme":
            cmd_theme(arg, session)
            continue
        if command == "/status":
            cmd_status()
            continue

        route(line)


if __name__ == "__main__":
    main()
