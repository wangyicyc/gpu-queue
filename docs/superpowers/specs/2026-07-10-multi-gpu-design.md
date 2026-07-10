# Design: Multi-GPU Scheduling for `gq`

**Date:** 2026-07-10
**Status:** Approved (pending spec review) — all design decisions confirmed via brainstorming

## Problem

`gq` currently assumes a single GPU: `gpu_is_idle()` returns a global bool ("any of my compute processes on any GPU"), the daemon runs one job at a time (blocking `_run_job`), and jobs don't declare GPU needs. On a multi-GPU server this wastes cards: a job on GPU 0 blocks the whole queue even when GPU 1–7 are idle.

The user wants true multi-GPU scheduling: multiple jobs run in parallel on different cards, gq auto-assigns idle cards, and multi-card jobs (DDP) are supported.

## Researched foundation (verified 2026-07-10)

Mainstream multi-GPU launchers all use **`CUDA_VISIBLE_DEVICES` as the standard mechanism to select which physical GPUs**:

| Launcher | Card-count param | Typical invocation |
|----------|------------------|--------------------|
| torchrun | `--nproc_per_node=N` | `CUDA_VISIBLE_DEVICES=0,1,2,3 torchrun --nproc_per_node=4 train.py` |
| accelerate | `--num_processes=N` | `CUDA_VISIBLE_DEVICES=0,1,2,3 accelerate launch --num_processes 4 train.py` |
| deepspeed | `--num_gpus=N` | `CUDA_VISIBLE_DEVICES=0,1,2,3 deepspeed --num_gpus 4 train.py` |

Key facts confirmed:
- `CUDA_VISIBLE_DEVICES` filters which physical GPUs are visible (renumbered 0..N-1 inside the process). All launchers respect it.
- The launcher's process-count param should equal the number of visible GPUs.
- `nvidia-smi pmon` reports **physical GPU indices** (column 1), unaffected by `CUDA_VISIBLE_DEVICES` remapping — verified by running a CUDA process and observing it appear on its physical card in `pmon`. So gq can accurately see which physical cards each process occupies.

Sources: [PyTorch DDP tutorial](https://docs.pytorch.org/tutorials/beginner/ddp_series_intro.html), [HuggingFace Accelerate launcher](https://huggingface.co/docs/accelerate/en/launcher).

## Design — all decisions confirmed

### Core model

1. **Multi-GPU parallelism.** Multiple jobs run simultaneously on different cards. The daemon does NOT block on one job; it launches a job whenever enough idle cards are available for the queue head, regardless of jobs already running.

2. **Card count is explicit: `gq add --gpus N 'cmd'`.** The user declares how many cards the job needs. gq picks N idle cards and injects `CUDA_VISIBLE_DEVICES=<those N physical indices>` into the job's environment. The command string is unchanged (gq only adds the env var, doesn't rewrite launcher params). Since N is user-declared, it always matches the launcher's process count (user's responsibility to keep `--gpus N` and `--nproc_per_node=N` consistent).

3. **No `--gpus` → default single-card (N=1) WITH a notice.** If the user runs `gq add 'cmd'` without `--gpus`, the job is treated as N=1, and `gq add` prints a one-line notice: `[gq] no --gpus given, running as single-card job`. (Not an error, not a warning that fires every time — just an informational line at add time, so a user who forgot `--gpus` on a multi-card job is reminded.)

4. **gq picks cards and injects `CUDA_VISIBLE_DEVICES`.** When launching a job needing N cards, gq picks N idle cards (first N idle by index) and runs with `CUDA_VISIBLE_DEVICES=<indices>` added to the env. All jobs get the injection — no escape hatch.

5. **Idle definition (unchanged).** A card is "idle (for me)" if no compute (`type=C`) process owned by the current user is on that card. Other users' processes do NOT block. Graphics (`type=G`) ignored. `nvidia-smi` failure/timeout → all cards treated as busy (safe default).

6. **Strict FIFO, no head-of-line skipping.** If the queue head needs N cards but fewer than N are idle, the daemon waits. Jobs behind it also wait. Accepted trade-off: a large job can block smaller ones.

7. **Full replacement.** gq becomes the multi-GPU scheduler; single-GPU degrades naturally (N=1, 1 card). No `--multi` flag, no two code paths.

8. **`gq stop <id>` (argument now required).** With multiple jobs running, `gq stop` requires a job ID and stops that specific job (SIGKILL its process group, releasing its cards). No-arg → error asking for an ID. Daemon stays alive (as today).

### GPU state model

Replace `gpu_is_idle() -> bool` with `busy_cards() -> set[int]`:
- Run `nvidia-smi pmon -s um -c 1`.
- For each non-comment line: parse gpu index (col 0), pid (col 1), type (col 2). Skip type != "C". If `/proc/<pid>` owner is the current user → add that gpu index to the busy set.
- Return busy set. Idle = all cards − busy.
- Total card count: `nvidia-smi -L` (count lines), cached at daemon start.
- Failure/timeout → return "all busy" sentinel (don't launch).

### Daemon loop (concurrent)

The current `_daemon_loop` blocks on `_run_job`. The new loop:
```
while not shutdown:
    busy = busy_cards()
    idle = all_cards - busy
    with locked_queue() as q:
        if q:
            head = q[0]
            n = head.get("n", 1)
            if len(idle) >= n:
                q.pop(0)
                assigned = sorted(idle)[:n]   # first N idle cards
                launch head with env += CUDA_VISIBLE_DEVICES=",".join(map(str,assigned))
                record running[head.id] = {job, cards: assigned, pid, started_at, n}
    sleep(poll_interval)
    # reap finished jobs (non-blocking): for each running job whose proc.poll() is not None,
    #   print DONE/FAILED, remove from running
```
- Jobs run as **concurrent subprocesses** (Popen, non-blocking). Daemon tracks all running Popen + assigned cards.
- Reaping: each poll cycle, `proc.poll()` each running job; if not None, print DONE/FAILED, remove from running. Cards release automatically (next `busy_cards()` won't see the gone process).

### `n` stored at add time

`gq add --gpus N` stores `n` in the job dict at add time (so the daemon reads `job["n"]`, no parsing at launch). Default (no `--gpus`) → `n=1` + the notice printed by `cmd_add`.

### State file changes

`state.json`:
```json
{
  "daemon_pid": 12345,
  "running": {
    "ab12": {"id":"ab12","cmd":"...","cwd":"...","env":{...},"n":2,"cards":[0,1],"pid":6789,"started_at":"..."},
    "cd34": {"id":"cd34","cmd":"...","cwd":"...","env":{...},"n":1,"cards":[2],"pid":6790,"started_at":"..."}
  }
}
```
- `running` changes from a single dict to a **dict keyed by job id** (empty when nothing running).
- Each entry adds `cards` (assigned physical indices) and `n`.
- Backward compat: old single-`running` `state.json` → cleared on next `gq watch` (treat as no running jobs). Old `queue.json` jobs without `n` → treated as `n=1` (`head.get("n", 1)`).

### Command changes

- **`gq add [--gpus N] 'cmd'`** — new optional `--gpus` (int, default 1). Stores `n` on the job. Without `--gpus`: n=1 + prints `[gq] no --gpus given, running as single-card job`. `env` snapshot still captured.
- **`gq list`** — running jobs show assigned cards: `ab12  torchrun ...   GPU 0,1   started 0:04:12 ago  [myenv]`. Queue rows unchanged (cards assigned at launch).
- **`gq stop <id>`** — argument required. Reads `state.running[id].pid`, SIGKILLs its process group, daemon reaps + releases cards. No-arg → error. Unknown id → error.
- **`gq cancel <id>`** / **`gq clear`** — unchanged.
- **`gq watch [--poll N]`** — interface unchanged; startup crash-recovery handles multiple orphaned running jobs.

### Crash recovery (cmd_watch startup)

On startup, if `state.running` is non-empty: for each entry, check pid's process group alive (`os.killpg(os.getpgid(pid), 0)`). Alive → orphan → `SIGKILL` its group, clear it. Dead → stale, clear it. (Today's logic, looped over all running entries.)

### Ctrl-C semantics (multi-job)

- **First Ctrl-C**: set `_shutdown_requested`. Stop launching NEW jobs. Let running jobs finish. (Same graceful behavior as today.)
- **Second Ctrl-C**: `SIGKILL` ALL running jobs' process groups, then exit. (Today kills one; now kills all running.)

## Testing strategy

pytest unit tests (monkeypatch `nvidia-smi`/`os.killpg`/`os.getpgid`/`subprocess.Popen`):

1. `cmd_add` with `--gpus 4` → job dict has `n=4`. Without `--gpus` → `n=1` + notice printed (`capsys`).
2. `busy_cards`: mock `nvidia-smi pmon` → correct busy set (own compute procs only, skip G type, skip other users). Returns set of ints.
3. Daemon loop: queue head `n=2`, 3 idle → launches, assigns 2 cards, records in state. 1 idle, head `n=2` → waits, no launch.
4. Concurrent: two jobs running on disjoint cards (mock Popen); one `poll()` returns non-None → reaped, removed from running; next head can use freed cards.
5. `gq stop <id>`: two running jobs, stops the named one only (kills its pgid), other keeps running.
6. `gq stop` (no arg) → error message, no kill.
7. Crash recovery: `state.running` with two entries, one alive one dead → kills the alive orphan, clears both.
8. `gq list` shows `GPU 0,1` for a 2-card running job.
9. `CUDA_VISIBLE_DEVICES` injection: launching a job with `n=2` and idle cards {0,1,2} → Popen called with env containing `CUDA_VISIBLE_DEVICES=0,1`.

Completion: `gq <Tab>` list unchanged (no new subcommand; `--gpus` is a flag on `add`, completion for `add` already does filename completion which is fine).

## Out of scope

- Parsing the command string for card count (card count is explicit via `--gpus`; no fragile parsing).
- `--no-inject` escape hatch (all jobs get `CUDA_VISIBLE_DEVICES` injected).
- Head-of-line skipping / backfilling (strict FIFO).
- Multi-user fairness (other users' processes don't block; contention accepted).
- Card memory/load-based scheduling (idle = no compute process of mine).
- Validating `--gpus N` against the launcher's `--nproc_per_node` (user's responsibility; N is explicit).
- Per-card queue / affinity (a job gets the first N idle cards; no preference logic).
- `gq stop` without arg (now requires `<id>`).

## Migration note

Full rewrite of the daemon loop, state model, `gpu_is_idle`→`busy_cards`, and `gq stop`. After merge, re-sync `~/.local/bin/gq` (per memory `gq-dev-install-sync`) and restart any running daemon. Old `queue.json` jobs work as-is (missing `n` → treated as 1); old single-`running` `state.json` cleared on next `gq watch`.
