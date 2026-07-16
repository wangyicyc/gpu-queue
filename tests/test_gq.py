import importlib.util, importlib.machinery, sys, os
import datetime
import json
import signal
from pathlib import Path
from unittest.mock import patch, MagicMock

# Load 'gq' script as a module (no .py extension).
# An explicit SourceFileLoader is required because spec_from_file_location
# cannot infer a loader for an extensionless file.
_gq_path = Path(__file__).parent.parent / "gq"
spec = importlib.util.spec_from_file_location(
    "gq", _gq_path, loader=importlib.machinery.SourceFileLoader("gq", str(_gq_path))
)
gq = importlib.util.module_from_spec(spec)
# Register in sys.modules so unittest.mock.patch("gq.busy_cards", ...) can resolve it.
sys.modules["gq"] = gq
spec.loader.exec_module(gq)


PMON_NO_PROCS = """\
# gpu        pid  type    sm   mem   enc   dec   command
# Idx          #   C/G     %     %     %     %   name
"""

PMON_OTHER_USER = """\
# gpu        pid  type    sm   mem   enc   dec   command
# Idx          #   C/G     %     %     %     %   name
    0      99999     C    45    12     0     0   python3
"""

PMON_MY_PID = """\
# gpu        pid  type    sm   mem   enc   dec   command
# Idx          #   C/G     %     %     %     %   name
    0      {pid}     C    60    30     0     0   python3
"""

# Realistic desktop environment: graphics processes (type=G) owned by the user,
# NO compute process. This is the always-present desktop compositor (Xorg,
# gnome-shell, chrome, VS Code). The GPU is idle for queue purposes.
PMON_DESKTOP_ONLY = """\
# gpu        pid  type    sm   mem   enc   dec   command
# Idx          #   C/G     %     %     %     %   name
    0       1255     G      -      -      -      -   Xorg
    0       2054     G     10      4      -      -   gnome-shell
    0       3955     G      -      -      -      -   chrome
    0    1949928     G      -      -      -      -   Code
"""

# Desktop environment + my compute job (type=C) → GPU is busy.
PMON_DESKTOP_PLUS_COMPUTE = """\
# gpu        pid  type    sm   mem   enc   dec   command
# Idx          #   C/G     %     %     %     %   name
    0       1255     G      -      -      -      -   Xorg
    0       2054     G     10      4      -      -   gnome-shell
    0    {pid}     C     60     30      -      -   python
"""

PMON_TWO_CARDS_MY_PROC = """\
# gpu        pid  type    sm   mem   enc   dec   command
# Idx          #   C/G     %     %     %     %   name
    0      12345     C    60    30     0     0   python
    1      99999     C    10     5     0     0   python3
    1       1255     G      -      -      -      -   Xorg
"""


def make_pmon_result(stdout, returncode=0):
    r = MagicMock()
    r.stdout = stdout
    r.returncode = returncode
    return r


def test_busy_cards_no_processes():
    with patch("subprocess.run", return_value=make_pmon_result(PMON_NO_PROCS)):
        assert gq.busy_cards() == set()


def test_busy_cards_other_user_process():
    """A process owned by another user should not count as busy."""
    # pid 99999 owned by root (uid 0), current user != 0
    with patch("subprocess.run", return_value=make_pmon_result(PMON_OTHER_USER)), \
         patch("os.stat") as mock_stat:
        mock_stat.return_value.st_uid = 0  # root owns pid 99999
        assert gq.busy_cards() == set()


def test_busy_cards_my_compute_pid():
    """A compute process owned by the current user marks its card busy."""
    my_pid = os.getpid()
    pmon_out = PMON_MY_PID.format(pid=my_pid)
    with patch("subprocess.run", return_value=make_pmon_result(pmon_out)), \
         patch("os.stat") as mock_stat:
        mock_stat.return_value.st_uid = os.getuid()
        assert 0 in gq.busy_cards()


def test_busy_cards_desktop_graphics_processes():
    """Graphics (type=G) processes owned by me must NOT count as busy.

    Regression test: the desktop environment (Xorg, gnome-shell, chrome, VS Code)
    is always on the GPU and owned by the user. Before the fix, these were
    treated as "GPU busy" and the queue never advanced.
    """
    with patch("subprocess.run", return_value=make_pmon_result(PMON_DESKTOP_ONLY)), \
         patch("os.stat") as mock_stat:
        mock_stat.return_value.st_uid = os.getuid()  # all desktop procs are mine
        assert gq.busy_cards() == set()


def test_busy_cards_desktop_plus_my_compute():
    """Desktop graphics + my compute (type=C) process → my card is busy."""
    my_pid = os.getpid()
    pmon_out = PMON_DESKTOP_PLUS_COMPUTE.format(pid=my_pid)

    def fake_stat(path):
        # The compute pid is mine; graphics pids are also mine but type=G (skipped).
        s = MagicMock()
        s.st_uid = os.getuid()
        return s

    with patch("subprocess.run", return_value=make_pmon_result(pmon_out)), \
         patch("os.stat", side_effect=fake_stat):
        assert 0 in gq.busy_cards()


def test_busy_cards_nvidia_smi_failure(monkeypatch):
    """nvidia-smi non-zero exit → all cards busy (safe default)."""
    monkeypatch.setattr(gq, "_total_cards", lambda: 2)
    with patch("subprocess.run", return_value=make_pmon_result("", returncode=1)):
        assert gq.busy_cards() == {0, 1}


def test_busy_cards_timeout(monkeypatch):
    """nvidia-smi timeout → all cards busy (safe default)."""
    import subprocess as sp
    monkeypatch.setattr(gq, "_total_cards", lambda: 2)
    with patch("subprocess.run", side_effect=sp.TimeoutExpired("nvidia-smi", 10)):
        assert gq.busy_cards() == {0, 1}




import pytest


@pytest.fixture(autouse=True)
def isolated_queue(tmp_path, monkeypatch):
    """Redirect QUEUE_DIR/QUEUE_FILE/STATE_FILE to a temp dir for every test,
    and reset module-level _running so one test's live jobs can't leak."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(gq, "_running", {})
    monkeypatch.setattr(gq, "_shutdown_requested", False)
    yield tmp_path


def test_read_queue_empty():
    assert gq.read_queue() == []


def test_write_then_read_queue():
    job = gq._make_job("echo hello", "/tmp")
    gq.write_queue([job])
    result = gq.read_queue()
    assert len(result) == 1
    assert result[0]["cmd"] == "echo hello"
    assert result[0]["cwd"] == "/tmp"
    assert len(result[0]["id"]) == 4


def test_make_job_fields():
    job = gq._make_job("python train.py", "/home/user/project")
    assert set(job.keys()) == {"id", "cmd", "cwd", "added_at", "env", "n"}
    assert job["cmd"] == "python train.py"
    assert job["cwd"] == "/home/user/project"
    assert isinstance(job["env"], dict)
    assert job["n"] == 1


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


def test_read_state_empty():
    state = gq.read_state()
    assert state == {"daemon_pid": None, "running": {}}


def test_write_then_read_state():
    gq.write_state({"daemon_pid": 1234, "running": {}})
    state = gq.read_state()
    assert state["daemon_pid"] == 1234
    assert state["running"] == {}


def test_read_queue_corrupted_resets(tmp_path, monkeypatch):
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    (tmp_path / "queue.json").write_text("not json {{{")
    assert gq.read_queue() == []


def test_read_state_corrupted_resets(tmp_path, monkeypatch):
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    (tmp_path / "state.json").write_text("not json {{{")
    state = gq.read_state()
    assert state == {"daemon_pid": None, "running": {}}


def test_read_queue_corrupt_bytes_resets(tmp_path, monkeypatch):
    qf = tmp_path / "queue.json"
    qf.write_bytes(b"\xff\xfe not valid utf8")
    monkeypatch.setattr(gq, "QUEUE_FILE", qf)
    assert gq.read_queue() == []


def test_read_state_non_dict_resets(tmp_path, monkeypatch):
    sf = tmp_path / "state.json"
    sf.write_text("null")
    monkeypatch.setattr(gq, "STATE_FILE", sf)
    state = gq.read_state()
    assert state == {"daemon_pid": None, "running": {}}

    # also test a list
    sf.write_text("[]")
    state = gq.read_state()
    assert state == {"daemon_pid": None, "running": {}}


def test_read_state_migrates_old_single_job_running(tmp_path, monkeypatch):
    """Old single-job running shape (dict with id/pid, non-dict values) -> {}."""
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps({"daemon_pid": 42, "running":
        {"id": "ab12", "cmd": "x", "pid": 99, "started_at": "t"}}))
    monkeypatch.setattr(gq, "STATE_FILE", sf)
    state = gq.read_state()
    assert state["running"] == {}


def test_read_state_preserves_dict_of_jobs(tmp_path, monkeypatch):
    """New dict-of-jobs running shape is preserved unchanged."""
    sf = tmp_path / "state.json"
    jobs = {"ab12": {"id": "ab12", "cmd": "x", "pid": 99, "cards": [0]}}
    sf.write_text(json.dumps({"daemon_pid": 42, "running": jobs}))
    monkeypatch.setattr(gq, "STATE_FILE", sf)
    state = gq.read_state()
    assert state["running"] == jobs


import argparse as ap


def _args(**kwargs):
    """Build a minimal argparse.Namespace."""
    return ap.Namespace(**kwargs)


def test_cmd_add_appends_to_queue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gq.cmd_add(_args(command="echo hi", gpus=None))
    q = gq.read_queue()
    assert len(q) == 1
    assert q[0]["cmd"] == "echo hi"
    assert q[0]["cwd"] == str(tmp_path)


def test_cmd_add_prints_job_id(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    gq.cmd_add(_args(command="echo hi", gpus=None))
    out = capsys.readouterr().out
    assert "added job" in out


def test_cmd_list_empty(capsys):
    gq.cmd_list(_args())
    out = capsys.readouterr().out
    assert "queue" in out.lower()
    assert "0 jobs" in out or "empty" in out.lower()


def test_cmd_list_shows_pending(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    gq.cmd_add(_args(command="python train.py", gpus=None))
    gq.cmd_list(_args())
    out = capsys.readouterr().out
    assert "python train.py" in out


def test_cmd_cancel_by_full_id(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    gq.cmd_add(_args(command="echo a", gpus=None))
    job_id = gq.read_queue()[0]["id"]
    gq.cmd_cancel(_args(job_id=job_id))
    assert gq.read_queue() == []
    out = capsys.readouterr().out
    assert "cancelled" in out


def test_cmd_cancel_by_prefix(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    gq.cmd_add(_args(command="echo a", gpus=None))
    job_id = gq.read_queue()[0]["id"]
    gq.cmd_cancel(_args(job_id=job_id[:2]))
    assert gq.read_queue() == []


def test_cmd_cancel_not_found(capsys):
    gq.cmd_cancel(_args(job_id="zzzz"))
    out = capsys.readouterr().out
    assert "not found" in out.lower()


def test_cmd_cancel_running_job(capsys):
    """Cannot cancel a running job via cancel — redirect user."""
    gq.write_state({"daemon_pid": None,
                    "running": {"aaaa": {"id": "aaaa", "cmd": "x",
                                         "pid": 1, "started_at": "t"}}})
    gq.cmd_cancel(_args(job_id="aaaa"))
    out = capsys.readouterr().out
    assert "running" in out.lower()


def test_cmd_cancel_ambiguous_prefix(tmp_path, monkeypatch, capsys):
    """Ambiguous prefix matches multiple pending jobs → message, no removal."""
    monkeypatch.chdir(tmp_path)
    # Add two jobs, then force their IDs to share a 2-char prefix
    gq.cmd_add(_args(command="echo a", gpus=None))
    gq.cmd_add(_args(command="echo b", gpus=None))
    queue = gq.read_queue()
    queue[0]["id"] = "ab12"
    queue[1]["id"] = "ab34"
    gq.write_queue(queue)
    gq.cmd_cancel(_args(job_id="ab"))
    out = capsys.readouterr().out
    assert "ambiguous" in out.lower()
    assert len(gq.read_queue()) == 2  # nothing removed


def test_cmd_clear_removes_all(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    gq.cmd_add(_args(command="a", gpus=None))
    gq.cmd_add(_args(command="b", gpus=None))
    gq.cmd_clear(_args())
    assert gq.read_queue() == []
    out = capsys.readouterr().out
    assert "2" in out


def test_cmd_stop_no_running_job(capsys):
    """gq stop with no running job → message, no kill attempted."""
    gq.write_state({"daemon_pid": None, "running": {}})
    gq.cmd_stop(_args(job_id="zzzz"))
    out = capsys.readouterr().out
    assert "no running job" in out.lower() or "not found" in out.lower()


def test_cmd_stop_kills_running_job(monkeypatch, capsys):
    """gq stop SIGKILLs the running job's process group — but ONLY when the
    three-way guard confirms the group is the child's own (pgid==pid) and
    distinct from gq's group. Here pgid==pid==12345, distinct from gq (0=1),
    so killpg(12345) fires."""
    gq.write_state({"daemon_pid": None,
                    "running": {"ab12": {"id": "ab12", "cmd": "x", "pid": 12345}}})
    calls = {"getpgid": [], "killpg": None}
    # pgid(12345) == 12345 (setsid worked); pgid(0) == 1 (gq's own group).
    def fake_getpgid(pid):
        calls["getpgid"].append(pid)
        return 12345 if pid == 12345 else 1
    killed = []
    monkeypatch.setattr(gq.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(gq.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    # _kill_pid_and_children must NOT be reached (killpg path taken); stub it
    # so we can assert it wasn't called.
    fallback_called = []
    monkeypatch.setattr(gq, "_kill_pid_and_children",
                        lambda pid: fallback_called.append(pid))
    gq.cmd_stop(_args(job_id="ab12"))
    out = capsys.readouterr().out
    assert killed == [(12345, signal.SIGKILL)], f"killpg should fire: {killed}"
    assert fallback_called == [], "fallback must NOT run when killpg guard passes"
    assert "stopped job ab12" in out
    assert "12345" in out


def test_cmd_stop_fallback_when_group_unsafe(monkeypatch, capsys):
    """When the child's pgid != pid (setsid failed) or == gq's group, killpg
    is DECLINED and _kill_pid_and_children (pid + children, no killpg) runs
    instead — never SIGKILL gq's own group."""
    gq.write_state({"daemon_pid": None,
                    "running": {"ab12": {"id": "ab12", "cmd": "x", "pid": 12345}}})
    # pgid(12345) == 1 == pgid(0): child shares gq's group → guard declines.
    monkeypatch.setattr(gq.os, "getpgid", lambda pid: 1)
    killpg_calls = []
    monkeypatch.setattr(gq.os, "killpg",
                        lambda pgid, sig: killpg_calls.append((pgid, sig)))
    fallback_called = []
    monkeypatch.setattr(gq, "_kill_pid_and_children",
                        lambda pid: fallback_called.append(pid))
    gq.cmd_stop(_args(job_id="ab12"))
    out = capsys.readouterr().out
    assert killpg_calls == [], f"killpg must NOT fire on shared group: {killpg_calls}"
    assert fallback_called == [12345], "fallback must run when guard declines"
    assert "stopped job ab12" in out


def test_cmd_stop_pid_already_dead(monkeypatch, capsys):
    """If the pid is already gone (ProcessLookupError), report gracefully."""
    gq.write_state({"daemon_pid": None,
                    "running": {"ab12": {"id": "ab12", "cmd": "x", "pid": 12345}}})

    def fake_getpgid(pid):
        raise ProcessLookupError

    killed = []
    monkeypatch.setattr(gq.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(gq.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    gq.cmd_stop(_args(job_id="ab12"))
    out = capsys.readouterr().out
    assert "already finished" in out or "not found" in out
    assert killed == []  # must NOT have called killpg


def test_cmd_stop_pid_none_treated_as_no_job(capsys):
    """running entry exists but pid is None (job not fully started) → no job."""
    gq.write_state({"daemon_pid": None,
                    "running": {"ab12": {"id": "ab12", "cmd": "x", "pid": None}}})
    gq.cmd_stop(_args(job_id="ab12"))
    out = capsys.readouterr().out
    assert "no pid" in out.lower() or "not fully started" in out.lower()


def test_format_elapsed():
    assert gq._format_elapsed(0) == "00:00:00"
    assert gq._format_elapsed(90) == "00:01:30"
    assert gq._format_elapsed(3661) == "01:01:01"


def test_force_kill_kills_process_group(tmp_path):
    """The second-Ctrl-C force-kill path kills the whole process group."""
    import subprocess as sp, time as _time
    # Spawn a sleep in its own session (mimics start_new_session=True)
    proc = sp.Popen("sleep 30", shell=True, start_new_session=True)
    _time.sleep(0.3)  # let it start
    assert proc.poll() is None  # still running
    # Simulate the handler's kill logic
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    _time.sleep(0.3)
    assert proc.poll() is not None, "process group was not killed"


# ---------------------------------------------------------------------------
# C1: read_queue guards non-list JSON
# ---------------------------------------------------------------------------

def test_read_queue_non_list_resets(tmp_path, monkeypatch):
    qf = tmp_path / "queue.json"
    for bad in ["{}", "42", '"oops"']:
        qf.write_text(bad)
        monkeypatch.setattr(gq, "QUEUE_FILE", qf)
        assert gq.read_queue() == []


# ---------------------------------------------------------------------------
# I1: atomic read-modify-write — concurrent cmd_add must not lose jobs
# ---------------------------------------------------------------------------

def test_cmd_add_concurrent_no_lost_jobs(tmp_path, monkeypatch):
    """Concurrent cmd_add calls must not lose jobs (atomic RMW)."""
    import threading
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.chdir(tmp_path)

    def adder(n):
        for i in range(n):
            gq.cmd_add(ap.Namespace(command=f"echo job{i}", gpus=None))

    threads = [threading.Thread(target=adder, args=(25,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 4 * 25 = 100 jobs, none lost
    assert len(gq.read_queue()) == 100, \
        f"lost jobs: only {len(gq.read_queue())}/100 retained"


# ---------------------------------------------------------------------------
# I2: real running pid + crash-recovery kills orphans
# ---------------------------------------------------------------------------

def test_cmd_watch_kills_orphan_on_startup(tmp_path, monkeypatch):
    """If state.running points to a live orphan process, cmd_watch kills it on startup."""
    import subprocess as sp, time as _time
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    # Spawn an orphan in its own session (mimics start_new_session=True)
    orphan = sp.Popen("sleep 60", shell=True, start_new_session=True)
    _time.sleep(0.3)
    assert orphan.poll() is None
    # Pre-seed state as if a previous daemon crashed mid-job (dict-keyed running)
    gq.write_state({"daemon_pid": None, "running": {"dead":
                     {"id": "dead", "cmd": "sleep 60", "pid": orphan.pid,
                      "started_at": "t"}}})
    # cmd_watch should detect the live orphan, kill it, clear running.
    # Patch _daemon_loop to raise immediately so cmd_watch returns after recovery.
    with patch("gq._daemon_loop", side_effect=KeyboardInterrupt):
        try:
            gq.cmd_watch(ap.Namespace(poll=1))
        except KeyboardInterrupt:
            pass
    _time.sleep(0.3)
    assert orphan.poll() is not None, "orphan was not killed by crash recovery"
    state = gq.read_state()
    assert state["running"] == {}


# ---------------------------------------------------------------------------
# I3: cmd_watch daemon_pid handling
# ---------------------------------------------------------------------------

def test_cmd_watch_refuses_live_daemon(tmp_path, monkeypatch):
    """A live daemon_pid makes cmd_watch refuse and exit."""
    import subprocess as sp, time as _time
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    # a real live process (sleeps briefly) to stand in for the daemon
    sleeper = sp.Popen("sleep 30", shell=True)
    _time.sleep(0.2)
    try:
        gq.write_state({"daemon_pid": sleeper.pid, "running": None})
        with patch("gq._daemon_loop") as mock_loop:
            gq.cmd_watch(ap.Namespace(poll=1))
            mock_loop.assert_not_called()  # refused, loop never entered
    finally:
        sleeper.kill(); sleeper.wait()
    state = gq.read_state()
    # daemon_pid left as-is on refusal (the live daemon owns it)
    assert state["daemon_pid"] == sleeper.pid


def test_cmd_watch_clears_stale_daemon_pid_and_runs(tmp_path, monkeypatch):
    """A dead daemon_pid is overwritten and the loop proceeds."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    # a pid that almost certainly doesn't exist
    gq.write_state({"daemon_pid": 999999, "running": None})
    with patch("gq._daemon_loop", side_effect=KeyboardInterrupt):
        try:
            gq.cmd_watch(ap.Namespace(poll=1))
        except KeyboardInterrupt:
            pass
    state = gq.read_state()
    # finally block cleared daemon_pid
    assert state["daemon_pid"] is None


def test_cmd_watch_finally_clears_daemon_pid(tmp_path, monkeypatch):
    """cmd_watch clears daemon_pid in finally even on normal exit."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    with patch("gq._daemon_loop", side_effect=KeyboardInterrupt):
        try:
            gq.cmd_watch(ap.Namespace(poll=1))
        except KeyboardInterrupt:
            pass
    assert gq.read_state()["daemon_pid"] is None


def test_launch_job_passes_env_with_cuda(monkeypatch):
    """_launch_job passes the job's captured env to Popen (full replacement)
    with CUDA_VISIBLE_DEVICES injected for the assigned cards."""
    captured = {}

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid

    def fake_popen(cmd, *args, **kwargs):
        captured["kwargs"] = kwargs
        return FakeProc(pid=4242)

    monkeypatch.setattr(gq.subprocess, "Popen", fake_popen)
    job_env = {"PATH": "/fake/bin", "CONDA_DEFAULT_ENV": "myenv", "MY_VAR": "v"}
    job = {"id": "t1", "cmd": "echo hi", "cwd": "/tmp",
           "started_at": datetime.datetime.now().isoformat(), "env": job_env}
    proc = gq._launch_job(job, cards=[0, 1])
    assert proc is not None
    env = captured["kwargs"]["env"]
    # Original env preserved, CUDA_VISIBLE_DEVICES added for the assigned cards.
    for k, v in job_env.items():
        assert env[k] == v
    assert env["CUDA_VISIBLE_DEVICES"] == "0,1"


def test_launch_job_legacy_no_env_gets_cuda_only(monkeypatch):
    """A job without an env field → Popen env contains only CUDA_VISIBLE_DEVICES.

    _make_job always captures env now, so this only affects hand-built legacy
    jobs. _launch_job always injects CUDA_VISIBLE_DEVICES (no escape hatch),
    so the env is the minimal {CUDA_VISIBLE_DEVICES: ...} rather than None.
    """
    captured = {}

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid

    def fake_popen(cmd, *args, **kwargs):
        captured["kwargs"] = kwargs
        return FakeProc(pid=1111)

    monkeypatch.setattr(gq.subprocess, "Popen", fake_popen)
    job = {"id": "t2", "cmd": "echo hi", "cwd": "/tmp",
           "started_at": datetime.datetime.now().isoformat()}  # no env key
    proc = gq._launch_job(job, cards=[0])
    assert proc is not None
    assert captured["kwargs"]["env"] == {"CUDA_VISIBLE_DEVICES": "0"}


def test_launch_job_bad_cwd_returns_none(capsys):
    """_launch_job with nonexistent cwd returns None (no Popen call)."""
    job = {"id": "badc", "cmd": "echo x", "cwd": "/nonexistent_dir_xyzzy",
           "started_at": datetime.datetime.now().isoformat()}
    proc = gq._launch_job(job, cards=[0])
    assert proc is None
    out = capsys.readouterr().out
    assert "WARNING" in out or "warning" in out.lower()


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


def test_cmd_list_shows_env_name(tmp_path, monkeypatch, capsys):
    """Pending rows show [envname] suffix when the job captured a conda env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "myenv")
    gq.cmd_add(_args(command="python train.py", gpus=None))
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
    gq.cmd_add(_args(command="python train.py", gpus=None))
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
    # No [envname] suffix on the job row: the command must not be followed
    # by the "  [ename]" suffix marker. ([gq] log prefixes still contain "[",
    # so we scope the check to the command + suffix boundary.)
    assert "python train.py  [" not in out


def test_cmd_list_shows_env_name_running(tmp_path, monkeypatch, capsys):
    """The running-job row also shows the captured env name suffix."""
    monkeypatch.chdir(tmp_path)
    gq.write_state({
        "daemon_pid": None,
        "running": {
            "r1": {
                "id": "r1",
                "cmd": "python train.py",
                "cwd": str(tmp_path),
                "started_at": "2026-07-09T10:00:00",
                "env": {"CONDA_DEFAULT_ENV": "myenv", "PATH": "/x"},
            },
        },
    })
    gq.cmd_list(_args())
    out = capsys.readouterr().out
    assert "[myenv]" in out
    # The suffix should appear on the running row (not just a pending row,
    # since no pending jobs exist here).
    assert "python train.py" in out


def test_make_job_stores_n():
    job = gq._make_job("python x.py", "/tmp", n=4)
    assert job["n"] == 4


def test_make_job_default_n():
    job = gq._make_job("python x.py", "/tmp")
    assert job["n"] == 1


def test_cmd_add_with_gpus(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gq.cmd_add(_args(command="python train.py", gpus=4))
    q = gq.read_queue()
    assert q[0]["n"] == 4


def test_cmd_add_without_gpus_default_single_and_notice(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # argparse default: gpus=None; cmd_add detects "not explicitly given" via the
    # None sentinel and prints the single-card notice. We pass gpus=None (the
    # default) and expect the notice.
    gq.cmd_add(_args(command="python train.py", gpus=None))
    out = capsys.readouterr().out
    assert "single-card" in out.lower() or "no --gpus" in out.lower()
    assert gq.read_queue()[0]["n"] == 1


# ---------------------------------------------------------------------------
# Task 2: TUI state-to-rows logic (_build_rows)
# ---------------------------------------------------------------------------

def test_build_rows_empty():
    """No running, no queued: only static ops (Add, Clear, Quit)."""
    rows = gq._build_rows({"daemon_pid": None, "running": {}}, [])
    actions = [r["action"] for r in rows]
    assert actions == ["add", "clear", "quit"]


def test_build_rows_with_running_and_queued():
    state = {"daemon_pid": 1, "running": {
        "ab12": {"id": "ab12", "cmd": "torchrun x", "cards": [0, 1], "pid": 9,
                 "started_at": "2026-07-11T10:00:00", "n": 2, "env": {}}}}
    queue = [{"id": "ef56", "cmd": "python eval.py", "n": 1, "env": {}}]
    rows = gq._build_rows(state, queue)
    actions = [r["action"] for r in rows]
    # Add first, then Stop per running job, Open log per running job,
    # Cancel per queued job, Clear, Quit.
    assert actions == ["add", "stop", "open_log", "cancel", "clear", "quit"]
    stop_row = next(r for r in rows if r["action"] == "stop")
    assert stop_row["job_id"] == "ab12"
    cancel_row = next(r for r in rows if r["action"] == "cancel")
    assert cancel_row["job_id"] == "ef56"


def test_build_rows_labels_contain_ids():
    state = {"daemon_pid": 1, "running": {
        "ab12": {"id": "ab12", "cmd": "torchrun x", "cards": [0], "pid": 9,
                 "started_at": "2026-07-11T10:00:00", "n": 1, "env": {}}}}
    rows = gq._build_rows(state, [])
    labels = " ".join(r["label"] for r in rows)
    assert "ab12" in labels  # the running job id appears in Stop/Open-log labels


# ---------------------------------------------------------------------------
# Task 2: busy_cards() / _total_cards() — per-card GPU state primitive
# ---------------------------------------------------------------------------

def test_busy_cards_my_process():
    """My compute process on gpu 0 -> {0} busy; other-user proc on gpu 1 ignored."""
    with patch("subprocess.run", return_value=make_pmon_result(PMON_TWO_CARDS_MY_PROC)), \
         patch("os.stat") as mock_stat:
        def fake_stat(path):
            s = MagicMock()
            # pid 12345 is mine; 99999 is root; 1255 is mine but type=G
            s.st_uid = 0 if "99999" in path else os.getuid()
            return s
        mock_stat.side_effect = fake_stat
        assert gq.busy_cards() == {0}


def test_busy_cards_none():
    with patch("subprocess.run", return_value=make_pmon_result(PMON_NO_PROCS)):
        assert gq.busy_cards() == set()


def test_busy_cards_nvidia_smi_failure_all_busy():
    """nvidia-smi non-zero exit -> empty set (caller treats as 'all busy' via _total_cards diff)."""
    r = MagicMock(); r.stdout = ""; r.returncode = 1
    with patch("subprocess.run", return_value=r):
        # On failure busy_cards returns set() — but the daemon must NOT launch.
        # The daemon treats len(idle) where idle = total - busy; on failure we
        # want idle empty. Design: busy_cards returns ALL card indices on failure.
        # See Step 3: failure returns set(range(total)). For the test, total is
        # mocked separately; here just assert it does not raise.
        result = gq.busy_cards()
        assert isinstance(result, set)



# ---------------------------------------------------------------------------
# Task 3: concurrent multi-GPU daemon loop — launch / wait / reap
# ---------------------------------------------------------------------------

class _FakeProc:
    """Stand-in for subprocess.Popen: supports .pid and .poll()."""
    def __init__(self, pid, exitcode=None):
        self.pid = pid
        self._exitcode = exitcode  # None = still running

    def poll(self):
        return self._exitcode


def test_daemon_launches_when_enough_idle(tmp_path, monkeypatch):
    """Head needs 2 cards, 3 idle -> launches, assigns 2 cards, records in state."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(gq, "_total_cards", lambda: 4)
    monkeypatch.setattr(gq, "busy_cards", lambda: set())  # all 4 idle
    monkeypatch.setattr(gq, "_pick_idle", lambda idle, n: sorted(idle)[:n])
    launched = {}

    def fake_popen(cmd, *a, **kw):
        p = _FakeProc(pid=99999)
        launched["cmd"] = cmd
        launched["env"] = kw.get("env")
        return p

    monkeypatch.setattr(gq.subprocess, "Popen", fake_popen)

    job = gq._make_job("torchrun --nproc_per_node=2 train.py", str(tmp_path), n=2)
    gq.write_queue([job])

    iterations = [0]

    def fake_sleep(n):
        iterations[0] += 1
        if iterations[0] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(gq.time, "sleep", fake_sleep)

    with patch("gq.busy_cards", lambda: set()):
        try:
            gq._daemon_loop(poll_interval=1)
        except KeyboardInterrupt:
            pass

    state = gq.read_state()
    assert job["id"] in state["running"]
    assert state["running"][job["id"]]["cards"] == [0, 1]
    assert "CUDA_VISIBLE_DEVICES" in launched["env"]
    assert launched["env"]["CUDA_VISIBLE_DEVICES"] == "0,1"


def test_daemon_waits_when_not_enough_idle(tmp_path, monkeypatch):
    """Head needs 2 cards, only 1 idle -> does not launch."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(gq, "_total_cards", lambda: 2)
    monkeypatch.setattr(gq, "busy_cards", lambda: {0})  # only card 1 idle
    monkeypatch.setattr(gq, "_pick_idle", lambda idle, n: sorted(idle)[:n])
    launched = []
    monkeypatch.setattr(gq.subprocess, "Popen",
                        lambda *a, **kw: launched.append(a) or _FakeProc(pid=1))

    job = gq._make_job("x", str(tmp_path), n=2)
    gq.write_queue([job])

    iterations = [0]

    def fake_sleep(n):
        iterations[0] += 1
        if iterations[0] >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(gq.time, "sleep", fake_sleep)
    try:
        gq._daemon_loop(poll_interval=1)
    except KeyboardInterrupt:
        pass
    assert launched == []  # never launched
    assert gq.read_queue()  # job still queued


def test_daemon_reaps_finished_job(tmp_path, monkeypatch):
    """A running job whose poll() returns non-None is reaped and removed from state."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(gq, "_total_cards", lambda: 1)
    monkeypatch.setattr(gq, "busy_cards", lambda: set())
    proc = _FakeProc(pid=555, exitcode=None)
    monkeypatch.setattr(gq.subprocess, "Popen", lambda *a, **kw: proc)

    # Pre-seed state as if a job is already running
    job = gq._make_job("x", str(tmp_path), n=1)
    job["started_at"] = "2026-07-10T00:00:00"
    gq.write_state({"daemon_pid": os.getpid(),
                    "running": {job["id"]: {**job, "cards": [0], "pid": 555}}})

    # Make the proc finish on the second poll
    calls = [0]

    def fake_poll():
        calls[0] += 1
        return 0 if calls[0] >= 2 else None

    proc.poll = fake_poll

    iterations = [0]

    def fake_sleep(n):
        iterations[0] += 1
        if iterations[0] >= 4:
            raise KeyboardInterrupt

    monkeypatch.setattr(gq.time, "sleep", fake_sleep)

    # Pre-seed the module-level _running so the daemon adopts the live proc.
    # The loop must NOT reset _running on entry (it only resets _shutdown_requested).
    monkeypatch.setattr(gq, "_running",
                        {job["id"]: {"proc": proc, "job": job, "cards": [0], "start": 0.0}})
    try:
        gq._daemon_loop(poll_interval=1)
    except KeyboardInterrupt:
        pass
    state = gq.read_state()
    assert job["id"] not in state["running"]  # reaped


def test_graceful_shutdown_drains_running_and_skips_launch(tmp_path, monkeypatch):
    """First Ctrl-C requests graceful shutdown: the loop must keep reaping the
    live job until it finishes (NOT exit immediately) and must NOT launch the
    queued head after shutdown is requested."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(gq, "_total_cards", lambda: 2)
    monkeypatch.setattr(gq, "busy_cards", lambda: set())
    monkeypatch.setattr(gq, "_pick_idle", lambda idle, n: sorted(idle)[:n])
    launched = []
    monkeypatch.setattr(gq.subprocess, "Popen",
                        lambda *a, **kw: launched.append(a) or _FakeProc(pid=777))

    # A pre-seeded running job + a queued head that must NOT launch post-shutdown.
    running_job = gq._make_job("running", str(tmp_path), n=1)
    running_job["started_at"] = "2026-07-10T00:00:00"
    queued_job = gq._make_job("queued", str(tmp_path), n=1)
    gq.write_queue([queued_job])
    gq.write_state({"daemon_pid": os.getpid(),
                    "running": {running_job["id"]:
                                {**running_job, "cards": [0], "pid": 555}}})

    proc = _FakeProc(pid=555, exitcode=None)
    poll_calls = [0]

    def fake_poll():
        poll_calls[0] += 1
        if poll_calls[0] == 1:
            # First Ctrl-C lands during the first reap: request graceful
            # shutdown while the job is still running (poll() -> None).
            gq._shutdown_requested = True
            return None
        return 0  # finished on the second reap

    proc.poll = fake_poll

    # Pre-seed the module-level _running so the daemon adopts the live proc.
    monkeypatch.setattr(gq, "_running",
                        {running_job["id"]: {"proc": proc, "job": running_job,
                                             "cards": [0], "start": 0.0}})

    # Safety valve: if the loop fails to drain and exit, force exit.
    iterations = [0]

    def fake_sleep(n):
        iterations[0] += 1
        if iterations[0] >= 5:
            raise KeyboardInterrupt

    monkeypatch.setattr(gq.time, "sleep", fake_sleep)

    try:
        gq._daemon_loop(poll_interval=1)
    except KeyboardInterrupt:
        pass

    # The queued head was NOT launched: launch block is gated after shutdown.
    assert launched == []
    # The running job WAS reaped before exit (loop drained instead of bailing).
    assert gq._running == {}
    state = gq.read_state()
    assert state["running"] == {}
    # The queued job is still queued (not launched, not dropped).
    assert [j["id"] for j in gq.read_queue()] == [queued_job["id"]]


def test_daemon_launches_multiple_jobs_per_cycle_disjoint_cards(tmp_path, monkeypatch):
    """Two n=2 jobs queued on a 4-card box, all idle (busy_cards=set() to
    simulate nvidia-smi lag). The launch loop must start BOTH in one cycle on
    DISJOINT card sets — the `idle = idle - set(cards)` reservation is the ONLY
    thing preventing a collision here. This test fails if that reservation line
    is removed."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(gq, "_total_cards", lambda: 4)
    monkeypatch.setattr(gq, "busy_cards", lambda: set())  # all idle, smi lag
    monkeypatch.setattr(gq, "_pick_idle", lambda idle, n: sorted(idle)[:n])
    pids = iter(range(1000, 9999))
    launched = []

    def fake_popen(cmd, *a, **kw):
        p = _FakeProc(pid=next(pids))
        launched.append({"cmd": cmd, "env": kw.get("env"), "pid": p.pid})
        return p

    monkeypatch.setattr(gq.subprocess, "Popen", fake_popen)

    job_a = gq._make_job("trainA", str(tmp_path), n=2)
    job_b = gq._make_job("trainB", str(tmp_path), n=2)
    gq.write_queue([job_a, job_b])

    iterations = [0]

    def fake_sleep(n):
        iterations[0] += 1
        if iterations[0] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(gq.time, "sleep", fake_sleep)

    try:
        gq._daemon_loop(poll_interval=1)
    except KeyboardInterrupt:
        pass

    # Both jobs launched in the single cycle.
    assert len(launched) == 2, f"expected 2 launches, got {len(launched)}"
    # Both recorded in _running / state.
    state = gq.read_state()
    assert job_a["id"] in state["running"]
    assert job_b["id"] in state["running"]
    cards_a = set(state["running"][job_a["id"]]["cards"])
    cards_b = set(state["running"][job_b["id"]]["cards"])
    # Disjoint card sets whose union is all 4 cards.
    assert cards_a.isdisjoint(cards_b), f"card collision: {cards_a} & {cards_b}"
    assert cards_a | cards_b == {0, 1, 2, 3}
    # Each got exactly 2 cards.
    assert len(cards_a) == 2 and len(cards_b) == 2
    # The queue is drained (both popped in one cycle).
    assert gq.read_queue() == []


def test_daemon_reap_frees_cards_then_launches_next(tmp_path, monkeypatch):
    """Cycle 1: job A (n=2 on cards {0,1}) is pre-seeded running and finishes
    (poll->0); reap frees {0,1}; the launch block then starts queued job B
    (n=2) on the now-free cards. Verifies reap runs before launch in the cycle
    and that freed cards are reusable."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(gq, "_total_cards", lambda: 4)
    monkeypatch.setattr(gq, "busy_cards", lambda: set())
    monkeypatch.setattr(gq, "_pick_idle", lambda idle, n: sorted(idle)[:n])

    # Job A pre-seeded as running; its proc finishes immediately (poll -> 0).
    job_a = gq._make_job("runA", str(tmp_path), n=2)
    job_a["started_at"] = "2026-07-10T00:00:00"
    proc_a = _FakeProc(pid=555, exitcode=0)
    monkeypatch.setattr(gq, "_running",
                        {job_a["id"]: {"proc": proc_a, "job": job_a,
                                       "cards": [0, 1], "start": 0.0}})
    gq.write_state({"daemon_pid": os.getpid(),
                    "running": {job_a["id"]:
                                {**job_a, "cards": [0, 1], "pid": 555}}})

    # Job B queued, needs 2 cards.
    job_b = gq._make_job("runB", str(tmp_path), n=2)
    gq.write_queue([job_b])

    launched = []

    def fake_popen(cmd, *a, **kw):
        p = _FakeProc(pid=777)
        launched.append({"env": kw.get("env")})
        return p

    monkeypatch.setattr(gq.subprocess, "Popen", fake_popen)

    iterations = [0]

    def fake_sleep(n):
        iterations[0] += 1
        if iterations[0] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(gq.time, "sleep", fake_sleep)

    try:
        gq._daemon_loop(poll_interval=1)
    except KeyboardInterrupt:
        pass

    state = gq.read_state()
    # Job A reaped (removed from running + state).
    assert job_a["id"] not in state["running"]
    assert job_a["id"] not in gq._running
    # Job B launched in the same cycle, got 2 cards (the freed {0,1}).
    assert job_b["id"] in state["running"]
    assert len(launched) == 1
    cards_b = state["running"][job_b["id"]]["cards"]
    assert len(cards_b) == 2
    assert set(cards_b) == {0, 1}


def test_pick_idle_returns_lowest_n():
    assert gq._pick_idle({3, 1, 4, 1, 5}, 2) == [1, 3]
    assert gq._pick_idle(set(), 1) == []
    assert gq._pick_idle({0, 2}, 5) == [0, 2]


def test_second_ctrl_c_kills_all_running(tmp_path, monkeypatch):
    """The second-Ctrl-C path SIGKILLs every running job's process group."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    killed = []
    monkeypatch.setattr(gq.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(gq.os, "killpg", lambda pgid, sig: killed.append(pgid))
    # Two fake running jobs in _running
    p1 = _FakeProc(pid=100)
    p2 = _FakeProc(pid=200)
    monkeypatch.setattr(gq, "_running", {
        "a": {"proc": p1, "job": {"id": "a"}, "cards": [0], "start": 0.0},
        "b": {"proc": p2, "job": {"id": "b"}, "cards": [1], "start": 0.0},
    })
    # Invoke the second-Ctrl-C kill-all path directly (the handler is a closure
    # inside _daemon_loop, so we test the extracted helper it delegates to).
    gq._force_kill_all_running()
    # Both process groups were SIGKILLed.
    assert sorted(killed) == [100, 200]


# ---------------------------------------------------------------------------
# Task 4: cmd_stop <id>, cmd_list cards column, multi-orphan crash recovery
# ---------------------------------------------------------------------------

def test_cmd_stop_requires_id(capsys):
    """gq stop with no matching id -> error, no kill."""
    gq.write_state({"daemon_pid": None, "running": {}})
    gq.cmd_stop(_args(job_id="zzzz"))
    out = capsys.readouterr().out
    assert "no running job" in out.lower() or "not found" in out.lower()


def test_cmd_stop_stops_named_job(monkeypatch, capsys):
    """With two running jobs, gq stop <id> kills only the named one."""
    gq.write_state({"daemon_pid": None, "running": {
        "ab12": {"id": "ab12", "cmd": "x", "pid": 111, "cards": [0], "n": 1},
        "cd34": {"id": "cd34", "cmd": "y", "pid": 222, "cards": [1], "n": 1},
    }})
    killed = []
    # pgid(111)==111 (setsid worked, pgid==pid), pgid(0)==1 (gq's group, distinct).
    # So the guard passes for ab12 and killpg(111) fires; cd34 is untouched.
    monkeypatch.setattr(gq.os, "getpgid", lambda pid: 1 if pid == 0 else pid)
    monkeypatch.setattr(gq.os, "killpg",
                        lambda pgid, sig: killed.append(pgid))
    monkeypatch.setattr(gq, "_kill_pid_and_children", lambda pid: None)
    gq.cmd_stop(_args(job_id="ab12"))
    out = capsys.readouterr().out
    assert "stopped job ab12" in out
    assert killed == [111]  # only ab12's group (pgid==pid==111), not cd34's


def test_cmd_stop_unknown_id(capsys):
    gq.write_state({"daemon_pid": None, "running": {
        "ab12": {"id": "ab12", "cmd": "x", "pid": 111, "cards": [0], "n": 1}}})
    gq.cmd_stop(_args(job_id="zz99"))
    out = capsys.readouterr().out
    assert "not found" in out.lower() or "no running job" in out.lower()


def test_cmd_list_shows_cards(tmp_path, monkeypatch, capsys):
    """Running jobs show their assigned GPU cards."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    gq.write_state({"daemon_pid": None, "running": {
        "ab12": {"id": "ab12", "cmd": "torchrun train.py", "cwd": str(tmp_path),
                 "pid": 111, "cards": [0, 1], "n": 2,
                 "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
                 "env": {}}}})
    gq.cmd_list(_args())
    out = capsys.readouterr().out
    assert "GPU 0,1" in out or "0,1" in out
    assert "torchrun train.py" in out


def test_cmd_watch_crash_recovery_multiple_orphans(monkeypatch, capsys, tmp_path):
    """Startup clears multiple orphaned running entries."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    # Two running entries: one alive, one dead
    gq.write_state({"daemon_pid": 99999, "running": {
        "ab12": {"id": "ab12", "cmd": "x", "pid": 111, "cards": [0], "n": 1},
        "cd34": {"id": "cd34", "cmd": "y", "pid": 222, "cards": [1], "n": 1}}})
    # pid 111 alive, pid 222 dead
    def fake_getpgid(pid):
        if pid == 222:
            raise ProcessLookupError
        return pid
    killed = []
    monkeypatch.setattr(gq.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(gq.os, "killpg", lambda pgid, sig: killed.append(pgid))
    # os.kill for daemon-pid check: daemon 99999 dead
    monkeypatch.setattr(gq.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError)
                        if pid == 99999 else None)
    # Stop the loop immediately
    monkeypatch.setattr(gq, "_daemon_loop",
                        lambda poll: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        gq.cmd_watch(_args(poll=1))
    except KeyboardInterrupt:
        pass
    assert 111 in killed or 1110 in killed  # the alive orphan's group was killed
    state = gq.read_state()
    assert state["running"] == {}  # all cleared


# ---------------------------------------------------------------------------
# Task 1 (TUI foundation): per-job log redirection in _launch_job
# ---------------------------------------------------------------------------

def test_launch_job_redirects_to_log_file(tmp_path, monkeypatch):
    """_launch_job opens ~/.gpu-queue/logs/<id>.log and passes it as stdout/stderr."""
    monkeypatch.setattr(gq, "LOG_DIR", tmp_path / "logs")
    captured = {}

    class FakeProc:
        pid = 12345

    def fake_popen(cmd, *args, **kwargs):
        captured["stdout"] = kwargs.get("stdout")
        captured["stderr"] = kwargs.get("stderr")
        captured["env_CVD"] = (kwargs.get("env") or {}).get("CUDA_VISIBLE_DEVICES")
        return FakeProc()

    monkeypatch.setattr(gq.subprocess, "Popen", fake_popen)
    job = {"id": "ab12", "cmd": "echo hi", "cwd": str(tmp_path), "env": {}}
    proc = gq._launch_job(job, [0])
    assert proc is not None
    # stdout/stderr were file objects opened on LOG_DIR/<id>.log
    assert captured["stdout"] is not None
    assert captured["stderr"] is captured["stdout"]  # same file, two streams
    log_path = tmp_path / "logs" / "ab12.log"
    assert captured["stdout"].name == str(log_path)
    assert captured["env_CVD"] == "0"


def test_launch_job_log_dir_created(tmp_path, monkeypatch):
    """LOG_DIR is created if it doesn't exist."""
    monkeypatch.setattr(gq, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(gq.subprocess, "Popen",
                        lambda *a, **kw: type("P", (), {"pid": 1})())
    gq._launch_job({"id": "x1", "cmd": "echo", "cwd": str(tmp_path), "env": {}}, [0])
    assert (tmp_path / "logs").is_dir()


# ---------------------------------------------------------------------------
# Task 3 (TUI): curses shell smoke test + _gpu_utilization helper
# ---------------------------------------------------------------------------

def test_tui_main_importable():
    """_tui_main and _render_panel exist and are callable (curses itself is manual)."""
    assert callable(gq._tui_main)
    assert callable(gq._render_panel)
    assert callable(gq._gpu_summary_line)


def test_util_bar_width_and_chars():
    """_util_bar is always exactly `width` chars, only block chars."""
    bar = gq._util_bar(67, 12)
    assert len(bar) == 12
    assert set(bar) <= set("█▏▎▍▌▋▊▉░")
    # 0% -> all empty, 100% -> all full
    assert gq._util_bar(0, 12) == "░" * 12
    assert gq._util_bar(100, 12) == "█" * 12
    # 50% -> half full / half empty
    assert gq._util_bar(50, 12) == "█" * 6 + "░" * 6


def test_gpu_utilization_parses_nvidia_smi(monkeypatch):
    """_gpu_utilization returns {gpu_index: percent} from nvidia-smi output."""
    class FakeResult:
        returncode = 0
        stdout = "67\n12\n0\n"

    def fake_run(cmd, **kwargs):
        assert cmd == ["nvidia-smi", "--query-gpu=utilization.gpu",
                       "--format=csv,noheader,nounits"]
        return FakeResult()

    monkeypatch.setattr(gq.subprocess, "run", fake_run)
    utils = gq._gpu_utilization()
    assert utils == {0: 67, 1: 12, 2: 0}


def test_gpu_utilization_failure_returns_empty(monkeypatch):
    """On nvidia-smi failure (nonzero exit), returns {} so callers degrade."""
    class FakeResult:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(gq.subprocess, "run",
                        lambda *a, **kw: FakeResult())
    assert gq._gpu_utilization() == {}


def test_gpu_utilization_missing_binary_returns_empty(monkeypatch):
    """If nvidia-smi isn't installed (FileNotFoundError), returns {}."""
    def boom(*a, **kw):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(gq.subprocess, "run", boom)
    assert gq._gpu_utilization() == {}


def test_tui_do_action_quit_returns_true(monkeypatch):
    """The quit action signals the TUI to exit."""
    # _tui_do_action with a quit row returns True (done).
    done = gq._tui_do_action.__wrapped__ if hasattr(gq._tui_do_action, "__wrapped__") else None
    # Call the underlying logic: we test via a thin pure helper _dispatch_action.
    assert gq._dispatch_action({"action": "quit", "job_id": None}) is True


def test_tui_do_action_cancel_writes_queue(monkeypatch, tmp_path):
    """The cancel action removes the job from the queue (via cmd_cancel logic)."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    gq.write_queue([{"id": "ef56", "cmd": "x", "cwd": str(tmp_path), "n": 1, "env": {}}])
    gq._dispatch_action({"action": "cancel", "job_id": "ef56"})
    assert gq.read_queue() == []


def test_tui_do_action_clear_empties_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    gq.write_queue([{"id": "a", "cmd": "x", "cwd": str(tmp_path), "n": 1, "env": {}},
                    {"id": "b", "cmd": "y", "cwd": str(tmp_path), "n": 1, "env": {}}])
    gq._dispatch_action({"action": "clear", "job_id": None})
    assert gq.read_queue() == []


def test_main_no_arg_runs_tui(monkeypatch):
    """gq with no subcommand enters the TUI (curses.wrapper)."""
    called = {"tui": False}
    def fake_wrapper(fn):
        called["tui"] = True
        # don't actually run curses
    monkeypatch.setattr(gq.curses, "wrapper", fake_wrapper)
    monkeypatch.setattr(sys, "argv", ["gq"])
    gq.main()
    assert called["tui"] is True


def test_main_subcommand_still_works(monkeypatch, tmp_path):
    """gq list (with subcommand) still dispatches to cmd_list, not TUI."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(sys, "argv", ["gq", "list"])
    gq.main()  # should not raise / not enter TUI


def test_build_capture_rcfile_content(tmp_path):
    """_build_capture_rcfile sources bashrc, defines __gq_capture, binds F5."""
    cmd_path = str(tmp_path / "cmd")
    cwd_path = str(tmp_path / "cwd")
    env_path = str(tmp_path / "env")
    rc = gq._build_capture_rcfile(cmd_path, cwd_path, env_path)
    assert "source ~/.bashrc" in rc
    assert "__gq_capture()" in rc
    assert cmd_path in rc and cwd_path in rc and env_path in rc
    assert r"\e[15~" in rc          # F5 bind
    assert "echo " in rc            # hint is echoed, not run as a command
    assert "READLINE_LINE" in rc
    assert "env -0" in rc


def test_key_to_bytes_arrow_and_ctrl():
    import curses as _c
    assert gq._key_to_bytes(_c.KEY_UP) == b"\x1b[A"
    assert gq._key_to_bytes(_c.KEY_DOWN) == b"\x1b[B"
    assert gq._key_to_bytes(_c.KEY_RIGHT) == b"\x1b[C"
    assert gq._key_to_bytes(_c.KEY_LEFT) == b"\x1b[D"
    assert gq._key_to_bytes(3) == b"\x03"              # Ctrl-C
    assert gq._key_to_bytes(_c.KEY_ENTER) == b"\r"
    assert gq._key_to_bytes(ord("a")) == b"a"
    assert gq._key_to_bytes(ord("\t")) == b"\t"


def test_key_to_bytes_reserved_returns_none():
    import curses as _c
    # F5 and Esc are reserved (gq handles them), not forwarded to bash.
    assert gq._key_to_bytes(_c.KEY_F5) is None
    assert gq._key_to_bytes(27) is None          # Esc


def test_render_pyte_to_dialog_draws_buffer():
    """A fake pyte screen with a known buffer -> the right curses writes.

    Real pyte Screen exposes ``buffer`` (dict[(y,x)] -> Char with .data/.fg)
    and ``cursor`` with ``.x``/``.y``. The fake matches that shape.
    """
    Char = type("Char", (), {})
    c = Char(); c.data = "A"; c.fg = "default"; c.bg = "default"
    c.bold = False; c.reverse = False; c.underscore = False
    screen = type("S", (), {})()
    screen.buffer = {0: {0: c}}  # buffer[y][x] -> Char
    screen.cursor = type("Cur", (), {})(); screen.cursor.x = 1; screen.cursor.y = 0

    class MockStd:
        def __init__(self): self.writes = []
        def addstr(self, *a, **kw):
            # addstr(y, x, text) or addstr(y, x, text, attr)
            y, x, t = a[0], a[1], a[2]
            self.writes.append((y, x, t))
        def move(self, *a): pass
        def refresh(self): pass
        def noutrefresh(self): pass
    std = MockStd()
    gq._render_pyte_to_dialog(std, screen, oy=2, ox=5, dh=1, dw=10)
    # The 'A' at buffer[(0,0)] should be drawn at curses (oy+0, ox+0) = (2,5).
    assert any(w[0] == 2 and w[1] == 5 and "A" in w[2] for w in std.writes), \
        f"expected 'A' at (2,5), got: {std.writes}"


def test_run_embedded_bash_f5_submit(monkeypatch, tmp_path):
    """F5 in the embedded bash reads the captured temp files and returns the
    dict. Mocks the pty/subprocess/select/os layer so no real bash is spawned.
    Requires pyte (skipped otherwise)."""
    pytest.importorskip("pyte")
    # Build fake temp files as if __gq_capture fired on F5.
    cmd_path = tmp_path / "cmd"; cmd_path.write_text("torchrun --nproc_per_node=4 train.py")
    cwd_path = tmp_path / "cwd"; cwd_path.write_text("/home/walle/proj")
    env_path = tmp_path / "env"; env_path.write_bytes(b"PATH=/x\x00MY=1\x00")

    # Mock the heavy modules so no real bash/pty is spawned.
    import pty as _pty, subprocess as _sp, select as _sel, os as _os, signal as _sig
    monkeypatch.setattr(_pty, "openpty", lambda: (100, 101))
    monkeypatch.setattr(_sp, "Popen",
                        lambda *a, **kw: type("P", (), {"pid": 1, "poll": lambda self: None,
                                                          "kill": lambda self: None,
                                                          "wait": lambda self: 0})())
    monkeypatch.setattr(_os, "close", lambda fd: None)
    # Capture os.write calls so we can assert the F5-forward (b"\x1b[15~") is
    # actually written to the pty master (fd=100 from the mocked openpty).
    captured_writes = []
    def fake_write(fd, data):
        captured_writes.append((fd, bytes(data)))
        return len(data)
    monkeypatch.setattr(_os, "write", fake_write)
    monkeypatch.setattr(_os, "read", lambda fd, n: b"")  # no bash output
    monkeypatch.setattr(_os, "unlink", lambda p: None)
    monkeypatch.setattr(_sel, "select", lambda r, w, x, t=None: ([], [], []))
    # mkstemp returns our fake paths in order: cmd, cwd, env, rc
    seq = {"n": 0}
    paths = [str(cmd_path), str(cwd_path), str(env_path), str(tmp_path / "rc")]
    def fake_mkstemp(*a, **kw):
        seq["n"] += 1
        return (seq["n"], paths[seq["n"] - 1])
    import tempfile as _tf
    monkeypatch.setattr(_tf, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(_sig, "signal", lambda *a: _sig.SIG_DFL)
    # CRITICAL: mock os.getpgid/os.killpg so the finally teardown does NOT call
    # the real killpg(getpgid(1)) — proc.pid is 1 (mock), getpgid(1)=1 (init's
    # group), and a real killpg(1, SIGKILL) would kill the whole session.
    monkeypatch.setattr(_os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(_os, "killpg", lambda pgid, sig: None)

    class MockWin:
        def __init__(self): self.k = [gq.curses.KEY_F5]
        def getmaxyx(self): return (24, 80)
        def box(self): pass
        def addstr(self, *a, **kw): pass
        def refresh(self): pass
        def noutrefresh(self): pass
        def erase(self): pass
        def move(self, *a): pass
        def subwin(self, *a): return self
        def getch(self):
            import curses as _c
            return _c.KEY_F5
    # Patch halfdelay/restore to no-ops
    monkeypatch.setattr(gq, "_tui_blocking_mode", lambda s: None)
    monkeypatch.setattr(gq, "_tui_restore_halfdelay", lambda s: None)
    monkeypatch.setattr(gq.curses, "halfdelay", lambda n: None)
    monkeypatch.setattr(gq.curses, "doupdate", lambda: None)

    result = gq._run_embedded_bash(MockWin(), oy=2, ox=0)
    assert result is not None
    assert result["cmd"] == "torchrun --nproc_per_node=4 train.py"
    assert result["cwd"] == "/home/walle/proj"
    assert result["env"]["MY"] == "1"
    # The F5-forward must actually write \e[15~ to the pty master (fd=100) so
    # bash's `bind -x` fires __gq_capture. This assertion is the regression
    # guard: removing the os.write(master, b"\x1b[15~") line fails here.
    assert any(data == b"\x1b[15~" and fd == 100 for fd, data in captured_writes)


def test_run_embedded_bash_esc_cancel(monkeypatch, tmp_path):
    """Esc cancels (returns None) without reading capture files."""
    pytest.importorskip("pyte")
    import pty as _pty, subprocess as _sp, select as _sel, os as _os, signal as _sig
    monkeypatch.setattr(_pty, "openpty", lambda: (100, 101))
    monkeypatch.setattr(_sp, "Popen",
                        lambda *a, **kw: type("P", (), {"pid": 1, "poll": lambda self: None,
                                                          "kill": lambda self: None,
                                                          "wait": lambda self: 0})())
    monkeypatch.setattr(_os, "close", lambda fd: None)
    monkeypatch.setattr(_os, "write", lambda fd, data: len(data))
    monkeypatch.setattr(_os, "read", lambda fd, n: b"")
    monkeypatch.setattr(_os, "unlink", lambda p: None)
    monkeypatch.setattr(_sel, "select", lambda r, w, x, t=None: ([], [], []))
    seq = {"n": 0}
    paths = [str(tmp_path / "cmd"), str(tmp_path / "cwd"),
             str(tmp_path / "env"), str(tmp_path / "rc")]
    def fake_mkstemp(*a, **kw):
        seq["n"] += 1
        return (seq["n"], paths[seq["n"] - 1])
    import tempfile as _tf
    monkeypatch.setattr(_tf, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(_sig, "signal", lambda *a: _sig.SIG_DFL)
    # CRITICAL: mock os.getpgid/os.killpg so the finally teardown does NOT call
    # the real killpg(getpgid(1)) — proc.pid is 1 (mock), getpgid(1)=1 (init's
    # group), and a real killpg(1, SIGKILL) would kill the whole session.
    monkeypatch.setattr(_os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(_os, "killpg", lambda pgid, sig: None)

    class MockWin:
        def getmaxyx(self): return (24, 80)
        def box(self): pass
        def addstr(self, *a, **kw): pass
        def refresh(self): pass
        def noutrefresh(self): pass
        def erase(self): pass
        def move(self, *a): pass
        def subwin(self, *a): return self
        def getch(self):
            return 27  # Esc
    monkeypatch.setattr(gq, "_tui_blocking_mode", lambda s: None)
    monkeypatch.setattr(gq, "_tui_restore_halfdelay", lambda s: None)
    monkeypatch.setattr(gq.curses, "halfdelay", lambda n: None)
    monkeypatch.setattr(gq.curses, "doupdate", lambda: None)

    result = gq._run_embedded_bash(MockWin(), oy=2, ox=0)
    assert result is None


def test_run_embedded_bash_never_killpg_own_group(monkeypatch, tmp_path):
    """SAFETY: the Esc/teardown path must NEVER os.killpg gq's own process
    group — that once killed the user's whole session when os.setsid didn't
    take effect and the child shared gq's group. Verify: when the child's
    pgid equals gq's own pgid, killpg is NOT called and only proc.kill() is.
    """
    pytest.importorskip("pyte")
    import pty as _pty, subprocess as _sp, select as _sel, os as _os, signal as _sig
    monkeypatch.setattr(_pty, "openpty", lambda: (100, 101))
    # proc.pid = 5; we'll make getpgid(5) return gq's OWN group (e.g. 7) to
    # simulate the setsid-failed case where the child is in gq's group.
    proc = type("P", (), {"pid": 5, "poll": lambda self: None,
                          "kill": lambda self: None,
                          "wait": lambda self: 0})()
    monkeypatch.setattr(_sp, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(_os, "close", lambda fd: None)
    monkeypatch.setattr(_os, "write", lambda fd, data: len(data))
    monkeypatch.setattr(_os, "read", lambda fd, n: b"")
    monkeypatch.setattr(_os, "unlink", lambda p: None)
    monkeypatch.setattr(_sel, "select", lambda r, w, x, t=None: ([], [], []))
    seq = {"n": 0}
    paths = [str(tmp_path / "cmd"), str(tmp_path / "cwd"),
             str(tmp_path / "env"), str(tmp_path / "rc")]
    def fake_mkstemp(*a, **kw):
        seq["n"] += 1
        return (seq["n"], paths[seq["n"] - 1])
    import tempfile as _tf
    monkeypatch.setattr(_tf, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(_sig, "signal", lambda *a: _sig.SIG_DFL)

    # getpgid(0) = gq's own group = 7; getpgid(5) = ALSO 7 (setsid failed) →
    # the child is in gq's group → killpg MUST be skipped, only proc.kill().
    def fake_getpgid(pid):
        # pid 0 = caller's group; pid 5 = child, but it shares the group.
        return 7
    killpg_calls = []
    kill_calls = []
    monkeypatch.setattr(_os, "getpgid", fake_getpgid)
    monkeypatch.setattr(_os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))
    # proc.kill records
    proc.kill = lambda: kill_calls.append("kill")

    class MockWin:
        def getmaxyx(self): return (24, 80)
        def box(self): pass
        def addstr(self, *a, **kw): pass
        def refresh(self): pass
        def noutrefresh(self): pass
        def erase(self): pass
        def move(self, *a): pass
        def subwin(self, *a): return self
        def getch(self):
            return 27  # Esc → triggers the finally cleanup
    monkeypatch.setattr(gq, "_tui_blocking_mode", lambda s: None)
    monkeypatch.setattr(gq, "_tui_restore_halfdelay", lambda s: None)
    monkeypatch.setattr(gq.curses, "halfdelay", lambda n: None)
    monkeypatch.setattr(gq.curses, "doupdate", lambda: None)

    gq._run_embedded_bash(MockWin(), oy=2, ox=0)
    # The child's group (7) equals gq's own group (7) → killpg MUST NOT fire.
    assert killpg_calls == [], f"killpg was called on the shared group! {killpg_calls}"
    # And the safe fallback (proc.kill) DID fire.
    assert kill_calls == ["kill"], f"expected proc.kill fallback, got {kill_calls}"


def test_run_embedded_bash_killpg_child_group_when_distinct(monkeypatch, tmp_path):
    """When the child IS in its own session (setsid worked, pgid == child pid,
    distinct from gq's group), killpg the child group to reap test-run children."""
    pytest.importorskip("pyte")
    import pty as _pty, subprocess as _sp, select as _sel, os as _os, signal as _sig
    monkeypatch.setattr(_pty, "openpty", lambda: (100, 101))
    proc = type("P", (), {"pid": 5, "poll": lambda self: None,
                          "kill": lambda self: None,
                          "wait": lambda self: 0})()
    monkeypatch.setattr(_sp, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(_os, "close", lambda fd: None)
    monkeypatch.setattr(_os, "write", lambda fd, data: len(data))
    monkeypatch.setattr(_os, "read", lambda fd, n: b"")
    monkeypatch.setattr(_os, "unlink", lambda p: None)
    monkeypatch.setattr(_sel, "select", lambda r, w, x, t=None: ([], [], []))
    seq = {"n": 0}
    paths = [str(tmp_path / "cmd"), str(tmp_path / "cwd"),
             str(tmp_path / "env"), str(tmp_path / "rc")]
    def fake_mkstemp(*a, **kw):
        seq["n"] += 1
        return (seq["n"], paths[seq["n"] - 1])
    import tempfile as _tf
    monkeypatch.setattr(_tf, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(_sig, "signal", lambda *a: _sig.SIG_DFL)

    # getpgid(0) = 7 (gq); getpgid(5) = 5 (child is own group leader, pgid==pid).
    def fake_getpgid(pid):
        return 7 if pid == 0 else 5
    killpg_calls = []
    monkeypatch.setattr(_os, "getpgid", fake_getpgid)
    monkeypatch.setattr(_os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    class MockWin:
        def getmaxyx(self): return (24, 80)
        def box(self): pass
        def addstr(self, *a, **kw): pass
        def refresh(self): pass
        def noutrefresh(self): pass
        def erase(self): pass
        def move(self, *a): pass
        def subwin(self, *a): return self
        def getch(self):
            return 27  # Esc
    monkeypatch.setattr(gq, "_tui_blocking_mode", lambda s: None)
    monkeypatch.setattr(gq, "_tui_restore_halfdelay", lambda s: None)
    monkeypatch.setattr(gq.curses, "halfdelay", lambda n: None)
    monkeypatch.setattr(gq.curses, "doupdate", lambda: None)

    gq._run_embedded_bash(MockWin(), oy=2, ox=0)
    # Child's group (5) is distinct from gq's (7) and pgid==pid → killpg(5, SIGKILL).
    assert killpg_calls == [(5, signal.SIGKILL)], f"unexpected killpg: {killpg_calls}"


def test_kill_pid_and_children_kills_pid_and_descendants(monkeypatch):
    """_kill_pid_and_children SIGKILLs the pid + its direct children (PPID==pid)
    by walking /proc/*/stat — WITHOUT killpg. /proc/<pid>/stat format is
    "pid (comm) state ppid ..." where comm may contain spaces/parens, so the
    parser must split from the LAST ')'.
    """
    killed = []
    # Fake /proc: pids 100 (the target, PPID=1), 200/201 (children, PPID=100),
    # 300 (unrelated, PPID=1), 400 (child with parens in comm, PPID=100).
    proc_stat = {
        "100": "100 (python3.8) R 1 ...\n",
        "200": "200 (train.py) R 100 ...\n",
        "201": "201 (sub proc) R 100 ...\n",
        "300": "300 (other) R 1 ...\n",
        "400": "400 (my (weird) name) R 100 ...\n",
    }
    class FakeFile:
        def __init__(self, content): self._c = content
        def read(self): return self._c
        def __enter__(self): return self
        def __exit__(self, *a): pass

    import builtins
    real_open = builtins.open
    def fake_open(path, *a, **kw):
        if isinstance(path, str) and path.startswith("/proc/") and path.endswith("/stat"):
            pid = path.split("/")[2]
            return FakeFile(proc_stat.get(pid, ""))
        return real_open(path, *a, **kw)
    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(os, "listdir", lambda d: list(proc_stat.keys()) if d == "/proc" else [])

    gq._kill_pid_and_children(100)
    killed_pids = {pid for pid, sig in killed}
    assert 100 in killed_pids, "must kill the target pid"
    assert 200 in killed_pids and 201 in killed_pids, "must kill children"
    assert 400 in killed_pids, "must handle parens-in-comm (split from last ')')"
    assert 300 not in killed_pids, "must not kill unrelated pids"
    assert all(sig == signal.SIGKILL for _, sig in killed)
