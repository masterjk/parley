#!/usr/bin/env python3
"""Preflight checks for parley runtime dependencies.

Run via `parley doctor` (or `bin/doctor.py` directly in a source checkout).
Each check prints `ok` or `fail` with actionable remediation text. Exit code
is non-zero if any check fails so this can run in CI.
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from parley_paths import (
    config_dir,
    cursors_dir,
    ensure_runtime_dirs,
    log_dir,
    state_dir,
)
from peek import parse_claude_line, parse_codex_line


HELP = {
    "tmux": "Install tmux 3.0+ (Homebrew: brew install tmux).",
    "python3": "Install Python 3.9+.",
    "prompt_toolkit": (
        "Install prompt_toolkit (pip install -r requirements.txt), or use the "
        "Homebrew formula's vendored environment."
    ),
    "claude": "Install or upgrade Claude Code: https://docs.claude.com/en/docs/claude-code/overview",
    "codex": "Install or upgrade Codex CLI: https://github.com/openai/codex",
}

# Where each agent writes its native session JSONLs. peek.py and discuss.py
# read these directly, so an agent that doesn't produce files matching these
# patterns (or whose records don't parse) is too old for parley.
SESSION_PATTERNS = {
    "claude": "~/.claude/projects/*/*.jsonl",
    "codex": "~/.codex/sessions/*/*/*/rollout-*.jsonl",
}
SESSION_PARSERS = {"claude": parse_claude_line, "codex": parse_codex_line}
SESSION_SAMPLE_LINES = 200


def version_output(cmd: str) -> str:
    for args in ([cmd, "--version"], [cmd, "-V"], [cmd, "version"]):
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = (proc.stdout or proc.stderr).strip()
        if proc.returncode == 0 and text:
            return " ".join(text.split())
    return "present, version unknown"


def check_write_dir(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".parley-check-", dir=path, delete=True):
            pass
        return True, str(path)
    except OSError as exc:
        return False, f"{path} ({exc})"


def check_tmux() -> tuple[bool, str]:
    if not shutil.which("tmux"):
        return False, HELP["tmux"]
    try:
        raw = subprocess.check_output(["tmux", "-V"], text=True, timeout=5).strip()
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"tmux exists but version check failed: {exc}"
    parts = raw.split()
    version = parts[1] if len(parts) > 1 else ""
    major = version.split(".", 1)[0]
    if major.isdigit() and int(major) < 3:
        return False, f"{raw}; need tmux 3.0+."
    return True, raw


def check_python() -> tuple[bool, str]:
    if not shutil.which("python3"):
        return False, HELP["python3"]
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 9):
        return False, f"python3 {version}; need 3.9+."
    return True, f"python3 {version}"


def check_prompt_toolkit() -> tuple[bool, str]:
    spec = importlib.util.find_spec("prompt_toolkit")
    if spec is None:
        return False, HELP["prompt_toolkit"]
    return True, "prompt_toolkit importable"


def newest_session(pattern: str) -> Path | None:
    matches = glob.glob(str(Path(pattern).expanduser()))
    if not matches:
        return None
    return max((Path(p) for p in matches), key=lambda p: p.stat().st_mtime)


def check_cli(cmd: str) -> tuple[bool, str]:
    if not shutil.which(cmd):
        return False, HELP[cmd]
    version = version_output(cmd)
    pattern = SESSION_PATTERNS[cmd]
    newest = newest_session(pattern)
    if newest is None:
        return True, (
            f"{version}; no session history yet at {pattern} — "
            f"run `{cmd}` once so parley can verify the JSONL format."
        )
    parser = SESSION_PARSERS[cmd]
    matched = 0
    try:
        with newest.open() as f:
            for i, line in enumerate(f):
                if i >= SESSION_SAMPLE_LINES:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if parser(obj):
                    matched += 1
                    if matched >= 3:
                        break
    except OSError as exc:
        return False, f"{version}; could not read {newest}: {exc}"
    if matched == 0:
        return False, (
            f"{version}; sampled {newest.name} but found no records in the "
            f"JSONL format peek/discuss expect — likely an outdated {cmd}. "
            f"{HELP[cmd]}"
        )
    return True, f"{version}; JSONL format ok ({newest.name})"


def run_checks() -> list[tuple[str, bool, str]]:
    ensure_runtime_dirs()
    checks = [
        ("tmux", *check_tmux()),
        ("python3", *check_python()),
        ("prompt_toolkit", *check_prompt_toolkit()),
        ("claude", *check_cli("claude")),
        ("codex", *check_cli("codex")),
    ]
    for label, path in (
        ("config dir", config_dir()),
        ("state dir", state_dir()),
        ("log dir", log_dir()),
        ("cursor dir", cursors_dir()),
    ):
        ok, detail = check_write_dir(path)
        checks.append((label, ok, detail))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="only print failures")
    args = parser.parse_args()

    checks = run_checks()
    failed = [(name, detail) for name, ok, detail in checks if not ok]

    if args.quiet:
        for name, detail in failed:
            print(f"parley: {name} check failed: {detail}", file=sys.stderr)
    else:
        for name, ok, detail in checks:
            mark = "ok" if ok else "fail"
            print(f"{mark:4} {name}: {detail}")
        if not failed:
            print("\nparley doctor: all checks passed")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
