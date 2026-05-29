#!/usr/bin/env python3
"""Dialogue orchestrator: makes Claude and Codex discuss a topic between themselves.

Usage:
  discuss.py <topic...>

Environment:
  CLAUDE_PANE, CODEX_PANE  tmux pane ids
  WORK_DIR                              parley project root

Behavior:
  1. Randomly choose a first speaker and send the kickoff with the topic.
  2. Poll Claude's session JSONL. When its turn is complete (quiescent for QUIET_SECS),
     extract assistant text, forward to Codex.
  3. Poll Codex's session JSONL similarly. Forward back to Claude.
  4. If a turn contains `[done]`, the orchestrator asks the other agent to confirm.
     Two-sided `[done]` is required to end the discussion.
  5. Hard stops: MAX_TURNS reached, RESPONSE_TIMEOUT exceeded waiting for an agent,
     SIGTERM received.
  6. Final result is written to the runtime dialog result file and logged.
"""
from __future__ import annotations

import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Tunables
QUIET_SECS = 3              # turn ends when JSONL stops changing this long
POLL_INTERVAL = 1.0
RESPONSE_TIMEOUT = 90       # give up if an agent takes longer than this
MAX_TURNS = 10
DONE_RE = re.compile(r"\[done\]", re.IGNORECASE)

from parley_paths import (
    cursors_dir,
    dialog_log_path,
    dialog_pid_path,
    dialog_result_path,
    ensure_runtime_dirs,
    latest_session_for,
)

BIN_DIR = Path(__file__).resolve().parent
WORK_DIR = Path(os.environ.get("WORK_DIR", BIN_DIR.parent))
ensure_runtime_dirs()
CURSORS_DIR = cursors_dir()
LOG_PATH = dialog_log_path()
RESULT_PATH = dialog_result_path()
PID_PATH = dialog_pid_path()

CLAUDE_PANE = os.environ.get("CLAUDE_PANE", "")
CODEX_PANE = os.environ.get("CODEX_PANE", "")


def log(msg: str) -> None:
    # stdout is redirected to LOG_PATH by relay.py — printing is enough.
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def session_path(agent: str) -> Path:
    """Resolve to the agent's CURRENT session JSONL.

    Discovered dynamically — agents (codex especially) rotate their session
    file silently mid-run, so we can't trust the startup pin alone.
    """
    path = latest_session_for(agent, str(WORK_DIR))
    if not path:
        sys.exit(f"No session file found for {agent}. start.sh must have run.")
    return path


def parse_claude_assistant_text(o: dict):
    """Return (ts, text) for an assistant message, or None to skip."""
    if o.get("type") != "assistant":
        return None
    ts = o.get("timestamp")
    content = o.get("message", {}).get("content")
    if not isinstance(content, list):
        return None
    texts = [item.get("text", "") for item in content
             if isinstance(item, dict) and item.get("type") == "text"]
    text = "\n".join(t for t in texts if t).strip()
    return (ts, text) if ts else None


def parse_codex_assistant_text(o: dict):
    if o.get("type") != "response_item":
        return None
    payload = o.get("payload", {})
    if payload.get("type") != "message":
        return None
    if payload.get("role") != "assistant":
        return None
    ts = o.get("timestamp")
    texts = [item.get("text", "") for item in payload.get("content", [])
             if isinstance(item, dict) and item.get("text")]
    text = "\n".join(t for t in texts if t).strip()
    return (ts, text) if ts else None


PARSERS = {"claude": parse_claude_assistant_text, "codex": parse_codex_assistant_text}


def read_assistant_turns(agent: str, after_ts: str):
    """All assistant turns with ts > after_ts. Returns list of (ts, text)."""
    parse = PARSERS[agent]
    out = []
    with open(session_path(agent)) as f:
        for line in f:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            r = parse(o)
            if r and r[0] > after_ts:
                out.append(r)
    return out


def wait_for_turn(agent: str, after_ts: str) -> tuple[str, str]:
    """Wait until `agent` produces a complete assistant turn after `after_ts`.

    A turn is complete when the session file has been quiescent for QUIET_SECS
    AND there is at least one assistant text entry after `after_ts`.
    Returns (latest_ts, concatenated_text). Raises TimeoutError after RESPONSE_TIMEOUT.
    """
    path = session_path(agent)
    deadline = time.time() + RESPONSE_TIMEOUT
    last_mtime = path.stat().st_mtime
    last_change = time.time()
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime != last_mtime:
            last_mtime = mtime
            last_change = time.time()
            continue
        # File quiescent. Has anything new come in?
        if time.time() - last_change < QUIET_SECS:
            continue
        turns = read_assistant_turns(agent, after_ts)
        if not turns:
            # Quiescent but no new assistant text. Keep waiting (agent might still be
            # mid-thinking or running tools; file mtime will resume changing).
            continue
        latest_ts = turns[-1][0]
        text = "\n\n".join(t for _, t in turns if t).strip()
        if not text:
            # Only non-text entries (tool calls). Keep waiting for actual text.
            continue
        return latest_ts, text
    raise TimeoutError(f"{agent} did not respond within {RESPONSE_TIMEOUT}s")


def send_to_pane(pane: str, msg: str) -> None:
    if not pane:
        log(f"WARN: empty pane id, skipping send")
        return
    subprocess.run(["tmux", "set-buffer", "--", msg], check=True)
    subprocess.run(["tmux", "paste-buffer", "-t", pane], check=True)
    time.sleep(0.3)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)


def current_max_ts(agent: str) -> str:
    """Highest assistant timestamp currently in the session — anchor for 'after this'."""
    turns = read_assistant_turns(agent, "")
    return turns[-1][0] if turns else ""


def format_forward(from_agent: str, turn: int, text: str, done_pending_from: str | None) -> str:
    header = f"[from {from_agent}, turn {turn}]"
    footer_lines = [
        "",
        "(Reply directly. Keep to 3-5 sentences. When you both agree, write `[done]`"
        " followed by a brief joint recommendation for Captain.)",
    ]
    if done_pending_from:
        footer_lines.insert(0, "")
        footer_lines.insert(1,
            f"NOTE: {done_pending_from} just proposed [done] with the recommendation above. "
            "If you agree, reply with `[done]` to confirm and end the discussion. "
            "If you disagree, continue normally and explain — do not write `[done]`."
        )
    return "\n".join([header, "", text, *footer_lines])


def cleanup() -> None:
    try:
        PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: discuss.py <topic...>")
    topic = " ".join(sys.argv[1:]).strip()
    if not topic:
        sys.exit("empty topic")

    if not (CLAUDE_PANE and CODEX_PANE):
        sys.exit("CLAUDE_PANE and CODEX_PANE env must be set")

    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()))
    RESULT_PATH.unlink(missing_ok=True)

    signal.signal(signal.SIGTERM, lambda *_: (log("SIGTERM received, exiting"), cleanup(), sys.exit(130)))
    signal.signal(signal.SIGINT,  lambda *_: (log("SIGINT received, exiting"),  cleanup(), sys.exit(130)))

    try:
        run(topic)
    finally:
        cleanup()


def run(topic: str) -> None:
    kickoff_template = (BIN_DIR / "prompts" / "discuss_kickoff.txt").read_text()
    kickoff = kickoff_template.replace("{topic}", topic)

    log(f"discussion armed. topic: {topic}")
    log(f"max turns: {MAX_TURNS}, response timeout: {RESPONSE_TIMEOUT}s")

    # Anchor cursors at the current latest assistant message so we ignore prior history.
    claude_cur = current_max_ts("claude")
    codex_cur = current_max_ts("codex")

    # Pick a random first speaker
    first = random.choice(["claude", "codex"])
    second = "codex" if first == "claude" else "claude"
    panes = {"claude": CLAUDE_PANE, "codex": CODEX_PANE}
    cursors = {"claude": claude_cur, "codex": codex_cur}

    log(f"turn 1: kicking off {first} with topic")
    send_to_pane(panes[first], kickoff)

    speakers = [first, second]

    done_proposed_by: str | None = None
    done_recommendation: str = ""
    final_text: str | None = None
    final_speaker: str | None = None

    for turn in range(1, MAX_TURNS + 1):
        speaker = speakers[(turn - 1) % 2]
        listener = speakers[turn % 2]
        log(f"turn {turn}: waiting for {speaker}")
        try:
            ts, text = wait_for_turn(speaker, cursors[speaker])
        except TimeoutError as e:
            log(f"timeout: {e}")
            summarize("timeout", turn, speaker, text=None, recommendation=done_recommendation)
            return
        cursors[speaker] = ts
        snippet = text.replace("\n", " ")[:120]
        log(f"turn {turn}: {speaker} replied — {snippet}...")

        contains_done = bool(DONE_RE.search(text))

        if done_proposed_by and done_proposed_by != speaker and contains_done:
            # Both have proposed [done] — discussion ends.
            log(f"turn {turn}: {speaker} confirmed [done]. discussion complete.")
            log(f"{done_proposed_by} proposed done. {speaker} confirmed. Final recommendation sent.")
            final_text = text
            final_speaker = speaker
            break
        elif done_proposed_by and done_proposed_by != speaker and not contains_done:
            log(f"turn {turn}: {speaker} did NOT confirm [done]. resuming dialogue.")
            done_proposed_by = None
            done_recommendation = ""
            # fall through to forward this turn normally
        elif not done_proposed_by and contains_done:
            done_proposed_by = speaker
            done_recommendation = text
            log(f"turn {turn}: {speaker} proposed [done]. awaiting {listener} confirmation.")
            # fall through to forward with done-pending flag

        if turn == MAX_TURNS:
            log(f"reached max turns ({MAX_TURNS}). stopping.")
            summarize("max-turns", turn, speaker, text=text, recommendation=done_recommendation)
            return

        # Forward to the listener
        msg = format_forward(speaker, turn, text, done_pending_from=done_proposed_by if done_proposed_by == speaker else None)
        send_to_pane(panes[listener], msg)

    summarize(
        "complete",
        turn,
        final_speaker,
        text=final_text,
        recommendation=final_text,
        proposed_by=done_proposed_by,
    )


def summarize(
    reason: str,
    turn: int,
    last_speaker: str | None,
    text: str | None,
    recommendation: str,
    proposed_by: str | None = None,
) -> None:
    lines = [
        "",
        "=" * 60,
        f"DISCUSSION ENDED ({reason}) after {turn} turn(s)",
        "=" * 60,
    ]
    if reason == "complete" and recommendation:
        if proposed_by and last_speaker:
            lines.append(f"\n{proposed_by} proposed done. {last_speaker} confirmed. Final recommendation sent.")
        lines.append(f"\nFinal recommendation (from {last_speaker}):\n")
        lines.append(recommendation.strip())
    elif reason == "max-turns":
        lines.append(f"\nLast position ({last_speaker}):\n")
        lines.append((text or "").strip())
    elif reason == "timeout":
        lines.append(f"\n{last_speaker or 'an agent'} timed out. Last recommendation in flight:\n")
        lines.append(recommendation or "(none)")
    lines.append("")
    lines.append("=" * 60)
    summary = "\n".join(lines)

    RESULT_PATH.write_text(summary)
    log(summary)


if __name__ == "__main__":
    main()
