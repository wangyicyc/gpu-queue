# Design: Per-job Shell Environment Capture

**Date:** 2026-07-08
**Status:** Approved (pending spec review)

## Problem

`gq` is a daemon-execution model, not an "adder-executes" model. When a user
runs `conda activate myenv` then `gq add 'python train.py'`:

- `gq add` is a short-lived process. It writes the command to `queue.json` and
  exits. The activated conda environment dies with that process.
- The job is later executed by the `gq watch` daemon — a long-lived process
  started in tmux, which runs in **its own** environment (base / the tmux
  shell's env).
- `_run_job` uses `subprocess.Popen(cmd, shell=True, cwd=cwd)` with no `env=`,
  so jobs inherit the **daemon's** environment, not the environment that was
  active when `gq add` was run.

Result: no matter what the user sources before `gq add`, the job does not run
in that environment. To make "activate then add → job runs in that env" work,
the environment must be **persisted into the queue** and **restored by the
daemon at execution time**.

## Design

Capture the full `os.environ` snapshot at `gq add` time, store it on the job,
and pass it to `Popen(env=...)` at execution time.

### Behavior

- `gq add` stores `dict(os.environ)` in a new `env` field on the job.
- `_run_job` runs with `env=job.get("env")`. Because `Popen`'s `env=` is a
  **full replacement** (not a merge), storing the complete dict is required —
  conda/venv activation sets many `CONDA_*` vars, rewrites `PATH`, and may set
  `LD_LIBRARY_PATH`; cherry-picking variables would silently drop some.
- Old jobs without an `env` field keep current behavior (`env=None` → inherit
  daemon env). Backward compatible.

### Data model

The job dict gains an optional `env: dict[str, str]` field:

```json
{
  "id": "ab12",
  "cmd": "python train.py --seed 1",
  "cwd": "/home/user/proj",
  "added_at": "2026-07-08T14:48:00",
  "env": { "PATH": "...", "CONDA_DEFAULT_ENV": "myenv", "...": "..." }
}
```

`queue.json` entries grow (envs are typically a few KB to tens of KB). This is
acceptable for a local single-user tool.

### Changes

**`gq`**

- `_make_job(cmd, cwd)`: add `env: dict(os.environ)`.
- `_run_job(job)`: `Popen(..., env=job.get("env"))`.
- `cmd_list`: show the captured env name after the command — read
  `CONDA_DEFAULT_ENV` first, else `VIRTUAL_ENV`'s basename, else nothing. This
  applies to both the running job and pending queue rows.

**`tests/test_gq.py`**

- `gq add` produces a job whose `env` equals the `os.environ` at add time.
- `_run_job` passes the captured `env` to `Popen` (monkeypatch `Popen` to
  capture kwargs).
- A legacy job with no `env` field → `Popen` called with `env=None`.
- `list` output includes the env name for a conda-env and venv job.

### Boundaries & trade-offs

- **Snapshot staleness:** the env is a point-in-time snapshot; if the env's
  install path changes later, the job does not auto-update. This matches the
  "run in the env I had when I added it" semantics, acceptable for local
  sweeps.
- **Secret exposure:** env may contain API keys, now persisted to
  `~/.gpu-queue/queue.json` (user-owned, local). One extra on-disk location
  beyond the shell env; negligible risk for local single-user. A `--no-env`
  opt-out is **explicitly deferred** (YAGNI) — keep it simple.
- No `--no-env` flag, no env-prefix filtering.
- Idle detection, file locking, crash recovery, signal handling are untouched.

## Testing strategy

Unit tests (pytest, existing style in `tests/test_gq.py`):

1. After `cmd_add`, the appended job's `env` is a dict equal to `os.environ`.
2. `_run_job` calls `Popen` with `env` equal to the job's captured env.
3. A job constructed without `env` → `Popen` called with `env=None` (backward
   compat).
4. `cmd_list` output contains the conda env name (`CONDA_DEFAULT_ENV`) and the
   venv name (`VIRTUAL_ENV` basename) where set.

## Out of scope

- `--no-env` flag (deferred).
- Env-prefix / variable filtering.
- Auto re-resolving stale env paths.
