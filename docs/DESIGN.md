# Parley Design

Living design document for the current tmux-based Claude + Codex relay.

## Concept

Parley starts one tmux session with Claude Code and Codex CLI in sibling panes,
plus a relay pane where Captain types once and routes each message to one or
both agents. It also supports a bounded agent-to-agent discussion mode that
returns a joint recommendation.

## Layout

Default launch layout:

```text
+-----------------------------------------+
| STATUS (1 line: app/version | edit owner | time | state)
+-----------------------------------------+
| Claude              | Codex
+-----------------------------------------+
| Captain's Pane: relay input, captain> prompt (15 lines)
+-----------------------------------------+
```

During `/discuss`, the relay opens a 10-line dialog strip above the relay pane:

```text
+-----------------------------------------+
| STATUS
+-----------------------------------------+
| Claude              | Codex
+-----------------------------------------+
| dialog strip (tail of discussion log)
+-----------------------------------------+
| relay
+-----------------------------------------+
```

The status strip is `bin/status.sh`. It refreshes continuously and reports the
current edit owner plus `INIT`, `READY`, or `DISCUSSING`.

## Addressing Rules

| Input form       | Routed to     | Notes                                  |
|------------------|---------------|----------------------------------------|
| `@claude <msg>`  | Claude only   | Codex never sees it                    |
| `@codex <msg>`   | Codex only    | Claude never sees it                   |
| `<msg>`          | Both agents   | No prefix means broadcast              |
| `@` alone        | Rejected      | Validator requires target + message    |
| `@<unknown>`     | Rejected      | Only `@claude` and `@codex` are valid  |

There is no sticky route mode: every message is either targeted with
`@claude`/`@codex` or broadcast to both. The agents do not share live memory.
If one needs context from the other's conversation with Captain, it must use
`bin/peek.py`.

## Input UX

- Typing `@` opens an autocomplete menu for `@claude` and `@codex`. Tab cycles
  the menu; typing `/` does the same for slash commands.
- Submitting a bare or unknown target is blocked by the prompt validator.
- Submitting a target with no message body is also blocked.
- Successful routing prints a compact acknowledgement such as `[to codex]`.
- `/discuss <topic>` starts an agent-to-agent debate; `/discuss off` stops it
  and closes the dialog strip.
- `/status` prints edit ownership, last route, and discussion status.
- `/quit` stops any discussion process and kills the tmux session.

When one agent needs to send context to the other, it asks Captain to relay
the message with `@claude <msg>` or `@codex <msg>` — there is no separate
handoff command.

## Agent Prompt Contract

Each agent gets a startup prompt from `bin/prompts/` explaining:

- It shares the workspace with Captain and the other agent.
- It must address the user as "Captain".
- Which routed messages it will and will not see.
- How to use `bin/peek.py` for opt-in cross-context visibility.
- How to use `bin/edit_owner.py` before and during file edits.
- How to ask Captain to relay a message when one agent needs to send context
  to the other.
- How discussion-mode turns and `[done]` confirmation work.

## Cross-Agent Visibility

Each agent can read the other's current native session JSONL through
`bin/peek.py`.

| Command                            | Returns                                      |
|------------------------------------|----------------------------------------------|
| `bin/peek.py <agent>`              | New messages since last peek                 |
| `bin/peek.py <agent> --tail N`     | Last N messages, ignoring cursor             |
| `bin/peek.py <agent> --all`        | Full parsed session                          |
| `bin/peek.py <agent> --since <ts>` | Messages newer than the ISO timestamp        |
| `bin/peek.py <agent> --no-update`  | Read without advancing the cursor            |
| `bin/peek.py <agent> --reader X`   | Use a reader-specific cursor                 |

Session identification:

- `bin/start.sh` snapshots existing session JSONLs before launching agents.
- After launch, a background poll finds the new Claude and Codex session files.
- Pinned session paths are written under the runtime cursor directory:
  `${XDG_STATE_HOME:-~/.local/state}/parley/cursors/`.
- Read cursors are cleared at startup.

Source data:

- Claude: `~/.claude/projects/<dashed-cwd>/<session-uuid>.jsonl`
- Codex: `~/.codex/sessions/YYYY/MM/DD/rollout-<iso-ts>-<uuid>.jsonl`
- `peek.py` filters to user/assistant text and summarizes tool-use entries.

## Discussion Mode

Captain starts a debate with `/discuss <topic>`.

1. `bin/relay.py` opens the dialog strip and starts `bin/discuss.py` in the
   background.
2. `bin/discuss.py` randomly chooses Claude or Codex as the first speaker and
   sends the kickoff prompt from `bin/prompts/discuss_kickoff.txt`.
3. The orchestrator polls the speaker's native session JSONL.
4. A turn is complete when the JSONL has been quiet for `QUIET_SECS` and new
   assistant text exists.
5. The completed turn is forwarded to the other agent with a
   `[from <agent>, turn N]` header.
6. Turns continue until a stop condition fires.

`[done]` semantics:

- A turn containing `[done]` is only a proposal.
- The other agent receives an explicit confirmation note.
- The discussion ends successfully only if the other agent also replies with
  `[done]`.
- If the other agent continues without `[done]`, the proposal is cleared.

Stop conditions:

- Two-sided `[done]` confirmation: `complete`
- `MAX_TURNS` reached, default 10: `max-turns`
- `RESPONSE_TIMEOUT` reached, default 90s: `timeout`
- `/discuss off`, `/quit`, or SIGTERM: process exits

Output:

- The dialog strip runs `tail -f` on the runtime discussion log.
- On two-sided `[done]`, the log names who proposed completion and who
  confirmed it.
- The final result is written to the runtime discussion result file.
- `/status` prints relay state, edit ownership, whether a discussion is active,
  and the last 10 discussion log lines.

## Edit Ownership

Edit ownership is a file-backed heartbeat managed by `bin/edit_owner.py`.

| Command                                      | Effect                         |
|----------------------------------------------|--------------------------------|
| `bin/edit_owner.py status`                   | Show the effective owner       |
| `bin/edit_owner.py claim codex "<scope>"`    | Mark Codex as actively editing |
| `bin/edit_owner.py heartbeat codex "<scope>"` | Refresh Codex ownership       |
| `bin/edit_owner.py release codex`            | Release ownership              |

Heartbeats older than `PARLEY_EDIT_OWNER_TTL` seconds, default 300, are shown
as idle. Captain can inspect or force-release ownership by running
`bin/edit_owner.py status` or `bin/edit_owner.py release --force` directly.

## Runtime Paths

Read-only code and prompts live under the install root. Mutable files are
created through `bin/parley_paths.py` and never need to live inside the repo.

| Purpose    | Path                                                        |
|------------|-------------------------------------------------------------|
| Config     | `${XDG_CONFIG_HOME:-~/.config}/parley`                      |
| State      | `${XDG_STATE_HOME:-~/.local/state}/parley`                  |
| Cursors    | `${XDG_STATE_HOME:-~/.local/state}/parley/cursors`          |
| Transcript | `${XDG_STATE_HOME:-~/.local/state}/parley/transcript.jsonl` |
| Edit owner | `${XDG_STATE_HOME:-~/.local/state}/parley/edit_owner.json`  |
| Result     | `${XDG_STATE_HOME:-~/.local/state}/parley/dialog.result`    |
| macOS logs | `~/Library/Logs/parley`                                    |
| Linux logs | `${XDG_STATE_HOME:-~/.local/state}/parley/logs`             |

`bin/parley_paths.py --shell-export` is the shell/Python boundary: shell
scripts use the exported `PARLEY_*` variables, and Python scripts import the
same helpers directly.

## File Map

```text
parley                      # top-level CLI wrapper; symlink-safe
requirements.txt            # Python dependency list for source installs
packaging/homebrew/         # Homebrew tap formula template
bin/
  start.sh                  # tmux layout, agent startup, session pinning
  relay.py                  # prompt_toolkit relay and command router
  peek.py                   # cursor-based reader for agent session JSONLs
  edit_owner.py             # file-backed edit-owner heartbeat
  discuss.py                # agent-to-agent discussion orchestrator
  doctor.py                 # preflight checks for dependencies and paths
  parley_paths.py           # runtime path source of truth
  status.sh                 # 1-line tmux status strip
  prompts/
    claude.txt
    codex.txt
    discuss_kickoff.txt
docs/
  DESIGN.md                 # this file
```

## Packaging

The top-level `parley` wrapper resolves symlinks before locating `bin/`, so it
works from a source checkout and from a Homebrew `bin/parley` shim. `parley
doctor` checks tmux, Python, `prompt_toolkit`, `claude`, `codex`, expected
session JSONL formats, and writable runtime directories.

Homebrew distribution is designed as a tap-first flow. The template at
`packaging/homebrew/parley.rb.template` installs scripts under `libexec`,
creates a private Python venv for Python dependencies, and leaves `claude` and
`codex` as external prerequisites detected by `parley doctor`.

## Architecture Decisions

- **Routing, not passive observation**: addressed messages go only to the
  target agent. This keeps attention clean, with `peek.py` as the explicit
  cross-context mechanism.
- **No MCP layer**: current cross-agent context is pull-only from native
  session JSONLs. There is no server, daemon, or cloud component.
- **Native session JSONLs over tmux capture logs**: structured agent logs avoid
  ANSI/TUI noise and reduce the amount of text agents need to inspect.
- **Snapshot-at-launch session detection**: comparing pre/post file listings is
  more deterministic than picking the newest session by mtime.
- **Python relay**: `prompt_toolkit` provides autocomplete, validation, and a
  better input loop than a bash `read` loop.
- **External mutable state**: runtime artifacts stay in XDG/macOS state and log
  locations so source checkouts and Homebrew installs remain read-only.
