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
  5. The discussion runs with no turn cap and no per-turn timeout — it ends only
     when both agents agree (`[done]` x2) or Captain stops it (SIGTERM, via
     `/discuss off`). Live progress (elapsed time, turn tallies, who we're
     waiting on) is published to the dialog state file for the discuss panel.
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
DONE_RE = re.compile(r"\[done\]", re.IGNORECASE)

from parley_paths import (
    cursors_dir,
    dialog_log_path,
    dialog_pid_path,
    dialog_result_path,
    dialog_state_path,
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
STATE_PATH = dialog_state_path()

CLAUDE_PANE = os.environ.get("CLAUDE_PANE", "")
CODEX_PANE = os.environ.get("CODEX_PANE", "")

# Live discussion state, mirrored to STATE_PATH for the discuss panel to render.
_state: dict = {}


def write_state(**updates) -> None:
    """Merge `updates` into the discussion state and write it atomically.

    The discuss panel polls STATE_PATH several times a second; an atomic
    write (temp file + os.replace) guarantees it never reads a half-written
    file and sees garbage.
    """
    _state.update(updates)
    _state["updated_at"] = time.time()
    tmp = STATE_PATH.parent / (STATE_PATH.name + ".tmp")
    try:
        tmp.write_text(json.dumps(_state))
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


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
    """Block until `agent` produces a complete assistant turn after `after_ts`.

    A turn is complete when the session file has been quiescent for QUIET_SECS
    AND there is at least one assistant text entry after `after_ts`.
    Returns (latest_ts, concatenated_text).

    There is no timeout: an agent may legitimately think or run tools for a
    long time, so we wait indefinitely. The discuss panel surfaces elapsed
    time so a human can decide when an agent is genuinely stuck and stop the
    discussion with `/discuss off`.
    """
    path = session_path(agent)
    last_mtime = path.stat().st_mtime
    last_change = time.time()
    while True:
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

    def on_stop(*_):
        log("stop signal received — ending discussion")
        write_state(phase="ended", ended_reason="stopped",
                    ended_at=time.time(), waiting_for=None, done_pending_by=None)
        cleanup()
        sys.exit(130)

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    try:
        run(topic)
    finally:
        cleanup()


def run(topic: str) -> None:
    kickoff_template = (BIN_DIR / "prompts" / "discuss_kickoff.txt").read_text()
    kickoff = kickoff_template.replace("{topic}", topic)

    log(f"discussion armed. topic: {topic}")
    log("no turn limit — runs until both agents agree or Captain stops it.")

    # Anchor cursors at the current latest assistant message so we ignore prior history.
    claude_cur = current_max_ts("claude")
    codex_cur = current_max_ts("codex")

    # Pick a random first speaker
    first = random.choice(["claude", "codex"])
    second = "codex" if first == "claude" else "claude"
    panes = {"claude": CLAUDE_PANE, "codex": CODEX_PANE}
    cursors = {"claude": claude_cur, "codex": codex_cur}
    counts = {"claude": 0, "codex": 0}
    speakers = [first, second]

    now = time.time()
    write_state(
        topic=topic,
        started_at=now,
        ended_at=None,
        turn=0,
        claude_turns=0,
        codex_turns=0,
        waiting_for=first,
        waiting_since=now,
        phase="waiting",
        done_pending_by=None,
        ended_reason=None,
    )

    log(f"turn 1: kicking off {first} with topic")
    send_to_pane(panes[first], kickoff)

    done_proposed_by: str | None = None
    done_recommendation: str = ""
    final_text: str | None = None
    final_speaker: str | None = None

    turn = 0
    while True:
        turn += 1
        speaker = speakers[(turn - 1) % 2]
        listener = speakers[turn % 2]
        write_state(turn=turn, waiting_for=speaker, waiting_since=time.time(),
                    phase="waiting", done_pending_by=done_proposed_by)
        log(f"turn {turn}: waiting for {speaker}")
        ts, text = wait_for_turn(speaker, cursors[speaker])
        cursors[speaker] = ts
        counts[speaker] += 1
        write_state(claude_turns=counts["claude"], codex_turns=counts["codex"],
                    waiting_for=listener, waiting_since=time.time(),
                    phase="forwarding")
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

        # Forward to the listener
        msg = format_forward(speaker, turn, text, done_pending_from=done_proposed_by if done_proposed_by == speaker else None)
        send_to_pane(panes[listener], msg)

    write_state(phase="ended", ended_reason="complete", ended_at=time.time(),
                waiting_for=None, done_pending_by=None)
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
    lines.append("")
    lines.append("=" * 60)
    summary = "\n".join(lines)

    RESULT_PATH.write_text(summary)
    log(summary)


if __name__ == "__main__":
    main()
