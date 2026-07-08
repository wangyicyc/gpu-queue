# Per-job Shell Environment Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `gq add` jobs run in the conda/venv environment the user had active when adding them, by capturing `os.environ` at add time and restoring it via `Popen(env=...)` at execution time.

**Architecture:** The job dict gains an `env` field holding a snapshot of `dict(os.environ)`. `_make_job` captures it; `_run_job` passes it to `subprocess.Popen(env=...)` (full replacement, not merge — so the whole dict is required). `cmd_list` reads env-name markers (`CONDA_DEFAULT_ENV`, else `VIRTUAL_ENV` basename) from the captured env and shows them. Old jobs without `env` keep current behavior (`env=None` → inherit daemon env).

**Tech Stack:** Python 3 stdlib only; pytest for tests. The `gq` script is loaded as an extensionless module in tests via `SourceFileLoader` (see top of `tests/test_gq.py`).

## Global Constraints

- **Zero external dependencies** — pure Python stdlib only (existing repo rule).
- **Single-file script** — all production changes go in the `gq` file at repo root; tests in `tests/test_gq.py`.
- **Backward compatible** — jobs without an `env` field must still run (inherit daemon env). No state-file format migration.
- **No new CLI flags** — `--no-env` / env filtering are explicitly out of scope (YAGNI).
- **Idle detection, file locking, crash recovery, signal handling are untouched.**
- Tests patch via `unittest.mock.patch` on string targets like `"subprocess.Popen"` and `"gq.gpu_is_idle"` (repo convention — `gq` is registered in `sys.modules` at test import).

---

## File Structure

- **Modify:** `gq` (repo root) — `_make_job` (capture env), `_run_job` (restore env), `cmd_list` (display env name), add `_env_name` helper.
- **Modify:** `tests/test_gq.py` — update `test_make_job_fields` (key set changes), add tests for capture/restore/list-display/legacy-compat.

No new files.

---

## Task 1: Capture `os.environ` in `_make_job`

**Files:**
- Modify: `gq` — `_make_job` function (currently lines 155-161)
- Test: `tests/test_gq.py` — update `test_make_job_fields` (line 157), add `test_make_job_captures_env`

**Interfaces:**
- Produces: `_make_job(cmd, cwd)` now returns a dict with an added key `"env": dict[str, str]` — a snapshot of `os.environ` at call time. Later tasks read `job["env"]` (in `_run_job`) and `job.get("env")` (in `_env_name`).

- [ ] **Step 1: Update the existing `test_make_job_fields` to expect the new key**

This existing test asserts the exact key set, so it must be updated first (it will fail otherwise).

In `tests/test_gq.py`, replace the body of `test_make_job_fields`:

```python
def test_make_job_fields():
    job = gq._make_job("python train.py", "/home/user/project")
    assert set(job.keys()) == {"id", "cmd", "cwd", "added_at", "env"}
    assert job["cmd"] == "python train.py"
    assert job["cwd"] == "/home/user/project"
    assert isinstance(job["env"], dict)
```

- [ ] **Step 2: Add a failing test that env is captured from `os.environ`**

Append this test to `tests/test_gq.py` (after `test_make_job_fields`):

```python
def test_make_job_captures_env(monkeypatch):
    """_make_job snapshots os.environ into the env field."""
    monkeypatch.setenv("GQ_TEST_ENV_VAR", "captured-value")
    job = gq._make_job("echo hi", "/tmp")
    assert job["env"]["GQ_TEST_ENV_VAR"] == "captured-value"
    # Full snapshot, not a cherry-pick
    assert job["env"] == dict(os.environ)
    # It's a copy — mutating the snapshot must not touch os.environ
    job["env"]["ANOTHER"] = "x"
    assert "ANOTHER" not in os.environ
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_gq.py::test_make_job_fields tests/test_gq.py::test_make_job_captures_env -v`
Expected: FAIL — `test_make_job_fields` fails on the key-set assertion; `test_make_job_captures_env` fails with `KeyError: 'env'` (or assertion that `'env' in job` is False).

- [ ] **Step 4: Implement — add the `env` field to `_make_job`**

In `gq`, replace the `_make_job` function:

```python
def _make_job(cmd: str, cwd: str) -> dict:
    return {
        "id": secrets.token_hex(2),
        "cmd": cmd,
        "cwd": cwd,
        "added_at": datetime.datetime.now().isoformat(timespec="seconds"),
        # Snapshot the caller's environment so the daemon can re-create it at
        # execution time. Popen(env=...) is a full replacement, not a merge, so
        # the entire dict must be stored (conda/venv activation rewrites PATH and
        # sets many CONDA_* / VIRTUAL_ENV vars that a cherry-pick would drop).
        "env": dict(os.environ),
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_gq.py::test_make_job_fields tests/test_gq.py::test_make_job_captures_env -v`
Expected: PASS (both).

- [ ] **Step 6: Commit**

```bash
git add gq tests/test_gq.py
git commit -m "feat(gq): capture os.environ snapshot in _make_job"
```

---

## Task 2: Restore the captured env in `_run_job`

**Files:**
- Modify: `gq` — `_run_job` function (currently the `subprocess.Popen(...)` call around line 269)
- Test: `tests/test_gq.py` — add `test_run_job_passes_env_to_popen`, `test_run_job_legacy_no_env`

**Interfaces:**
- Consumes: `job["env"]` produced by Task 1.
- Produces: `_run_job` now calls `subprocess.Popen(cmd, shell=True, cwd=cwd, start_new_session=True, env=job.get("env"))`. When `env` is absent → `None` → Popen inherits the daemon env (legacy behavior preserved).

- [ ] **Step 1: Write failing test — captured env is passed to Popen**

Append to `tests/test_gq.py`:

```python
def test_run_job_passes_env_to_popen(monkeypatch):
    """_run_job passes the job's captured env to Popen (full replacement)."""
    captured = {}

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid
        def wait(self):
            return 0

    def fake_popen(cmd, *args, **kwargs):
        captured["kwargs"] = kwargs
        return FakeProc(pid=4242)

    monkeypatch.setattr(gq.subprocess, "Popen", fake_popen)
    job_env = {"PATH": "/fake/bin", "CONDA_DEFAULT_ENV": "myenv", "MY_VAR": "v"}
    job = {"id": "t1", "cmd": "echo hi", "cwd": "/tmp",
           "started_at": datetime.datetime.now().isoformat(), "env": job_env}
    gq._run_job(job)
    assert captured["kwargs"]["env"] == job_env


def test_run_job_legacy_no_env(monkeypatch):
    """A job without an env field → Popen called with env=None (inherit daemon env)."""
    captured = {}

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid
        def wait(self):
            return 0

    def fake_popen(cmd, *args, **kwargs):
        captured["kwargs"] = kwargs
        return FakeProc(pid=1111)

    monkeypatch.setattr(gq.subprocess, "Popen", fake_popen)
    job = {"id": "t2", "cmd": "echo hi", "cwd": "/tmp",
           "started_at": datetime.datetime.now().isoformat()}  # no env key
    gq._run_job(job)
    assert captured["kwargs"]["env"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_gq.py::test_run_job_passes_env_to_popen tests/test_gq.py::test_run_job_legacy_no_env -v`
Expected: FAIL — `test_run_job_passes_env_to_popen` fails because `captured["kwargs"]` has no `env` key (KeyError); `test_run_job_legacy_no_env` similarly.

- [ ] **Step 3: Implement — pass env to Popen**

In `gq`, in `_run_job`, the current Popen call is:

```python
        proc = subprocess.Popen(job["cmd"], shell=True, cwd=cwd,
                                start_new_session=True)
```

Replace it with:

```python
        proc = subprocess.Popen(job["cmd"], shell=True, cwd=cwd,
                                start_new_session=True,
                                env=job.get("env"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gq.py::test_run_job_passes_env_to_popen tests/test_gq.py::test_run_job_legacy_no_env -v`
Expected: PASS (both).

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS (the count goes up by the new tests; the existing `test_run_job_*` tests still pass because they construct jobs and rely on real/inherit env behavior — `test_run_job_success`/`test_run_job_failure`/`test_run_job_bad_cwd` build jobs without `env`, so `env=None` is passed and behavior is unchanged).

- [ ] **Step 6: Commit**

```bash
git add gq tests/test_gq.py
git commit -m "feat(gq): restore captured env via Popen(env=...)"
```

---

## Task 3: Display the env name in `cmd_list`

**Files:**
- Modify: `gq` — add `_env_name` helper; update `cmd_list` running-job row (line ~185) and pending-row print (line ~196)
- Test: `tests/test_gq.py` — add `test_env_name_conda`, `test_env_name_venv`, `test_env_name_none`, `test_cmd_list_shows_env_name`

**Interfaces:**
- Consumes: `job["env"]` produced by Task 1.
- Produces: `_env_name(job) -> str` — returns `CONDA_DEFAULT_ENV` if set in the captured env, else `os.path.basename(VIRTUAL_ENV)` if set, else `""`. Used only by `cmd_list` for display.

- [ ] **Step 1: Write failing tests for the `_env_name` helper**

Append to `tests/test_gq.py`:

```python
def test_env_name_conda():
    job = {"env": {"CONDA_DEFAULT_ENV": "myenv", "PATH": "/x"}}
    assert gq._env_name(job) == "myenv"


def test_env_name_venv():
    job = {"env": {"VIRTUAL_ENV": "/home/u/.venvs/ml", "PATH": "/x"}}
    assert gq._env_name(job) == "ml"


def test_env_name_none():
    assert gq._env_name({}) == ""
    assert gq._env_name({"env": {"PATH": "/x"}}) == ""
    # CONDA_DEFAULT_ENV takes precedence over VIRTUAL_ENV
    job = {"env": {"CONDA_DEFAULT_ENV": "conda", "VIRTUAL_ENV": "/v"}}
    assert gq._env_name(job) == "conda"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_gq.py::test_env_name_conda tests/test_gq.py::test_env_name_venv tests/test_gq.py::test_env_name_none -v`
Expected: FAIL — `AttributeError: module 'gq' has no attribute '_env_name'`.

- [ ] **Step 3: Implement — add `_env_name` helper**

In `gq`, add this helper just above `cmd_list` (after `_format_elapsed`):

```python
def _env_name(job: dict) -> str:
    """Return a short human-readable name for the job's captured environment.

    Prefers CONDA_DEFAULT_ENV (set by conda activate), else the basename of
    VIRTUAL_ENV (set by venv/activate). Returns "" if neither is present.
    Reads from the job's captured env snapshot, NOT os.environ — the daemon
    runs in a different environment than the shell that added the job.
    """
    env = job.get("env") or {}
    if env.get("CONDA_DEFAULT_ENV"):
        return env["CONDA_DEFAULT_ENV"]
    venv = env.get("VIRTUAL_ENV")
    if venv:
        return os.path.basename(venv)
    return ""
```

- [ ] **Step 4: Run the helper tests to verify they pass**

Run: `python -m pytest tests/test_gq.py::test_env_name_conda tests/test_gq.py::test_env_name_venv tests/test_gq.py::test_env_name_none -v`
Expected: PASS (all three).

- [ ] **Step 5: Write failing test — `cmd_list` shows env name for pending jobs**

Append to `tests/test_gq.py`:

```python
def test_cmd_list_shows_env_name(tmp_path, monkeypatch, capsys):
    """Pending rows show [envname] suffix when the job captured a conda env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "myenv")
    gq.cmd_add(_args(command="python train.py"))
    gq.cmd_list(_args())
    out = capsys.readouterr().out
    assert "python train.py" in out
    assert "[myenv]" in out


def test_cmd_list_shows_venv_name(tmp_path, monkeypatch, capsys):
    """Pending rows show [basename] for a venv job."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VIRTUAL_ENV", "/home/u/.venvs/ml")
    # Ensure no conda var is set so venv path wins
    monkeypatch.delenv("CONDA_DEFAULT_ENV", raising=False)
    gq.cmd_add(_args(command="python train.py"))
    gq.cmd_list(_args())
    out = capsys.readouterr().out
    assert "[ml]" in out


def test_cmd_list_no_env_suffix_when_no_env(tmp_path, monkeypatch, capsys):
    """A legacy job (no env) shows no suffix."""
    monkeypatch.chdir(tmp_path)
    # Build a legacy job directly, bypassing _make_job's env capture
    gq.write_queue([{"id": "ab12", "cmd": "python train.py", "cwd": str(tmp_path),
                     "added_at": "2026-07-08T00:00:00"}])
    gq.cmd_list(_args())
    out = capsys.readouterr().out
    assert "python train.py" in out
    assert "[" not in out
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `python -m pytest tests/test_gq.py::test_cmd_list_shows_env_name tests/test_gq.py::test_cmd_list_shows_venv_name tests/test_gq.py::test_cmd_list_no_env_suffix_when_no_env -v`
Expected: FAIL — the env-name tests fail (`[myenv]` / `[ml]` not in output); `test_cmd_list_no_env_suffix_when_no_env` may already pass (no suffix today) but is included for regression safety.

- [ ] **Step 7: Implement — show env name in `cmd_list` rows**

In `gq`, in `cmd_list`, the running-job row currently is:

```python
        print(f"  {r['id']}  {r['cmd']}   started {elapsed_str} ago")
```

Replace with:

```python
        ename = _env_name(r)
        suffix = f"  [{ename}]" if ename else ""
        print(f"  {r['id']}  {r['cmd']}   started {elapsed_str} ago{suffix}")
```

And the pending-row print currently is:

```python
        print(f"  #{i}  {job['id']}  {job['cmd']}")
```

Replace with:

```python
        ename = _env_name(job)
        suffix = f"  [{ename}]" if ename else ""
        print(f"  #{i}  {job['id']}  {job['cmd']}{suffix}")
```

- [ ] **Step 8: Run the cmd_list tests to verify they pass**

Run: `python -m pytest tests/test_gq.py::test_cmd_list_shows_env_name tests/test_gq.py::test_cmd_list_shows_venv_name tests/test_gq.py::test_cmd_list_no_env_suffix_when_no_env -v`
Expected: PASS (all three).

- [ ] **Step 9: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS. Note: `test_cmd_list_shows_pending` (existing) adds a job and asserts `"python train.py" in out` — still passes; if the test runner happens to be inside a conda env, the row now has a `[envname]` suffix but the substring assertion still holds.

- [ ] **Step 10: Commit**

```bash
git add gq tests/test_gq.py
git commit -m "feat(gq): show captured env name in list output"
```

---

## Task 4: Document the behavior in README

**Files:**
- Modify: `README.md` — both the 中文 and English sections

- [ ] **Step 1: Add a note to the 中文 section**

Under the 中文 `### 特点` list, the "任意 shell 命令" bullet is currently:

```
- **任意 shell 命令**：`python train.py`、`bash run.sh` 都能排队
```

Replace with:

```
- **自动带上你的环境**：`gq add` 时会快照当前 `conda`/`venv` 环境,daemon 执行时原样还原。`conda activate myenv` 后再 `gq add 'python train.py'`,任务就在 `myenv` 里跑
- **任意 shell 命令**：`python train.py`、`bash run.sh` 都能排队
```

- [ ] **Step 2: Add a note to the English section**

Under the English `### Features` list, the "Any shell command" bullet is currently:

```
- **Any shell command** — `python train.py`, `bash run.sh`, anything
```

Replace with:

```
- **Carries your environment** — `gq add` snapshots the current `conda`/`venv` env and the daemon restores it at execution time. `conda activate myenv` then `gq add 'python train.py'` runs the job inside `myenv`
- **Any shell command** — `python train.py`, `bash run.sh`, anything
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: note that gq add carries the active conda/venv env"
```

---

## Self-Review

**1. Spec coverage:**
- "Capture `os.environ` at `gq add`" → Task 1 (`_make_job`). ✓
- "Pass to `Popen(env=...)` at execution" → Task 2 (`_run_job`). ✓
- "Old jobs without env keep behavior (`env=None`)" → Task 2 `test_run_job_legacy_no_env` + `job.get("env")`. ✓
- "Data model: optional `env` field" → Task 1. ✓
- "`cmd_list` shows env name (`CONDA_DEFAULT_ENV` else `VIRTUAL_ENV` basename)" → Task 3. ✓
- "No `--no-env` flag, no filtering" → not implemented (correctly out of scope). ✓
- "Idle detection / locking / crash recovery / signals untouched" → no edits to those code paths. ✓
- "Tests: capture, restore, legacy, list display" → Tasks 1-3. ✓
- "README note about env + secret exposure mention" → Task 4 covers the env note. The secret-exposure caveat was a design discussion note, not a required doc artifact; the README already documents `~/.gpu-queue/` as the state location. No additional task required.

**2. Placeholder scan:** No TBD/TODO. Every code step has full code. Test code is complete.

**3. Type consistency:** `_env_name(job)` signature is consistent across Task 3's tests and the helper definition. `job["env"]` / `job.get("env")` usage is consistent with Task 1's `_make_job` output. The Popen call adds `env=job.get("env")` matching the test's `captured["kwargs"]["env"]` assertions.

No gaps found. Plan is complete.
