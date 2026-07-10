# Design: `gq stop` — Stop the Running Job from Any Window

**Date:** 2026-07-10
**Status:** Approved (pending spec review)

## Problem

The only way to stop a *running* job today is to switch to the daemon terminal and press Ctrl-C twice (once graceful, once force-kill). `gq cancel` refuses running jobs. This is awkward when the `gq add` shell and the daemon are different windows/tmux panes — you have to context-switch to find the daemon.

The user wants a "more graceful" way: from any window, run one command to stop the currently-running job, while the daemon stays alive and moves on to the next queued job.

## Existing mechanism (verified)

`gq` already records the real subprocess pid in `state.json`:

- `_run_job` (gq:302-309): after `Popen(start_new_session=True)`, sets `_current_job_pid = proc.pid` and writes `_state["running"]["pid"] = proc.pid`.
- The crash-recovery block in `cmd_watch` (gq:417-443) already reads `running.pid`, calls `os.getpgid(rpid)`, and `os.killpg(..., signal.SIGKILL)` to reap orphans. **This is exactly the operation `gq stop` needs** — same pid source, same kill mechanism.

`start_new_session=True` means the job's shell+command form their own process group where `getpgid(pid) == pid`, so `killpg` cleans up the real command (not just the `/bin/sh -c` wrapper). Killing only the shell pid would orphan the python process and leave the GPU occupied.

## Design

### Behavior

New `gq stop` subcommand (no arguments). Run from any window:

1. Read `state.json`, get `running` and its `pid`.
2. **No running job** (`running` is None, or `pid` is None/missing) → print `[gq] no job currently running.` and exit (no error, no daemon impact).
3. **Running job with a pid** → `os.killpg(os.getpgid(pid), signal.SIGKILL)` to kill the whole process group. Print `[gq] stopped job <id> (pid <pid>).`
4. The daemon is **not** signaled and is **not** killed. It is blocked in `proc.wait()` (gq:314); when the killed process reaps, `wait()` returns a non-zero code, the daemon prints `<<< job <id> FAILED`, clears `running` (gq:392), and continues the poll loop to pick up the next queued job.

### Edge cases / error handling

- **`ProcessLookupError`** from `getpgid`/`killpg` (pid already dead, or race where the job finished between read and kill) → print `[gq] job <id> already finished (pid <pid> not found).` Not an error.
- **`PermissionError`** from `getpgid`/`killpg` (pid not ours — shouldn't happen since we started it) → print `[gq] ERROR: cannot stop job <id> (pid <pid>): <exc>`.
- **`TypeError`** (pid is None/non-int) → treated as "no running job" (same as case 2).
- No `--force` flag, no `gq stop <id>` argument: at most one job runs at a time, so "stop the current one" is unambiguous. YAGNI.

### Why this is clean (no daemon changes)

The daemon needs zero changes. It already:
- publishes the pid to `state.json` (gq:308),
- blocks on `proc.wait()` which returns when the process is killed externally,
- clears `running` and continues after any job ends (gq:392, gq:394).

`gq stop` is a pure "external observer that reads state and signals the process group" — symmetric with the existing crash-recovery code. No IPC, no signal-to-daemon channel, no daemon awareness of being stopped.

### Files

- **`gq`**: add `cmd_stop(args)` (mirrors the crash-recovery kill block), register `stop` subcommand in `main()`.
- **`tests/test_gq.py`**: 
  - no running job → prints "no job currently running", no kill attempted
  - running job with pid → calls `os.killpg(os.getpgid(pid), SIGKILL)` with the right pid (monkeypatch both, capture args)
  - pid already dead (`ProcessLookupError` from `getpgid`) → prints "already finished", no crash
  - pid None (running entry exists but no pid yet) → treated as no running job
- **`README.md`** (中文 + English): add `gq stop` to the command table, command reference, and a "common scenario" entry ("stop a misbehaving running job"); **also fix the tmux framing** — currently implies tmux is required; change to "tmux is optional, only needed if you close the terminal / want it to survive logout" (per the user's finding that tmux is not mandatory).
- **`completions/gq.bash`**: add `stop` to the subcommand list (it's a no-arg subcommand like `list`/`clear`, so no special completion branch needed).
- **`tests/test_completion.sh`**: the subcommand-list test asserts the exact set, so add `stop` to the expected list.

### Relationship to Ctrl-C

Ctrl-C twice in the daemon terminal still works (it kills the job AND exits the daemon). `gq stop` kills the job but keeps the daemon alive — the "more graceful" option the user asked for. Both use `killpg(SIGKILL)` under the hood; no conflict.

## Testing strategy

pytest unit tests (monkeypatch `os.killpg`, `os.getpgid`, and `read_state`):

1. `cmd_stop` with no `running` in state → output contains "no job currently running"; `os.killpg` NOT called.
2. `cmd_stop` with `running: {id, pid: 12345}` → `os.getpgid(12345)` and `os.killpg(<pgid>, SIGKILL)` called; output contains "stopped job".
3. `cmd_stop` where `os.getpgid` raises `ProcessLookupError` → output contains "already finished"; `os.killpg` NOT called; no exception.
4. `cmd_stop` with `running: {id, pid: None}` → treated as no running job (no kill).

Completion test update: `gq <Tab>` expected list becomes `watch add list cancel clear stop`.

## Out of scope

- `gq stop <id>` (argument form) — at most one job runs; YAGNI.
- `--force` flag — SIGKILL is already force; no graceful SIGTERM escalation (training scripts don't handle SIGTERM, so it adds complexity for no benefit).
- Stopping the daemon itself via `gq stop` (use Ctrl-C in the daemon terminal for that).
- Auto-detecting "is the pid really mine" beyond PermissionError (out of scope; we started the job).
