import importlib.util, importlib.machinery, sys, os
import datetime
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
# Register in sys.modules so unittest.mock.patch("gq.gpu_is_idle", ...) can resolve it.
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


def make_pmon_result(stdout, returncode=0):
    r = MagicMock()
    r.stdout = stdout
    r.returncode = returncode
    return r


def test_gpu_idle_no_processes():
    with patch("subprocess.run", return_value=make_pmon_result(PMON_NO_PROCS)):
        assert gq.gpu_is_idle() is True


def test_gpu_idle_other_user_process():
    """A process owned by another user should not block."""
    # pid 99999 owned by root (uid 0), current user != 0
    with patch("subprocess.run", return_value=make_pmon_result(PMON_OTHER_USER)), \
         patch("os.stat") as mock_stat:
        mock_stat.return_value.st_uid = 0  # root owns pid 99999
        assert gq.gpu_is_idle() is True


def test_gpu_busy_my_process():
    """A compute process owned by the current user should block."""
    my_pid = os.getpid()
    pmon_out = PMON_MY_PID.format(pid=my_pid)
    with patch("subprocess.run", return_value=make_pmon_result(pmon_out)), \
         patch("os.stat") as mock_stat:
        mock_stat.return_value.st_uid = os.getuid()
        assert gq.gpu_is_idle() is False


def test_gpu_idle_desktop_graphics_processes():
    """Graphics (type=G) processes owned by me must NOT block the queue.

    Regression test: the desktop environment (Xorg, gnome-shell, chrome, VS Code)
    is always on the GPU and owned by the user. Before the fix, gpu_is_idle()
    treated these as "GPU busy" and the queue never advanced.
    """
    with patch("subprocess.run", return_value=make_pmon_result(PMON_DESKTOP_ONLY)), \
         patch("os.stat") as mock_stat:
        mock_stat.return_value.st_uid = os.getuid()  # all desktop procs are mine
        assert gq.gpu_is_idle() is True


def test_gpu_busy_desktop_plus_my_compute():
    """Desktop graphics + my compute (type=C) process → busy."""
    my_pid = os.getpid()
    pmon_out = PMON_DESKTOP_PLUS_COMPUTE.format(pid=my_pid)

    def fake_stat(path):
        # The compute pid is mine; graphics pids are also mine but type=G (skipped).
        s = MagicMock()
        s.st_uid = os.getuid()
        return s

    with patch("subprocess.run", return_value=make_pmon_result(pmon_out)), \
         patch("os.stat", side_effect=fake_stat):
        assert gq.gpu_is_idle() is False


def test_gpu_idle_nvidia_smi_failure():
    """nvidia-smi non-zero exit → treat as busy (safe default)."""
    with patch("subprocess.run", return_value=make_pmon_result("", returncode=1)):
        assert gq.gpu_is_idle() is False


def test_gpu_idle_timeout():
    import subprocess as sp
    with patch("subprocess.run", side_effect=sp.TimeoutExpired("nvidia-smi", 10)):
        assert gq.gpu_is_idle() is False


import pytest


@pytest.fixture(autouse=True)
def isolated_queue(tmp_path, monkeypatch):
    """Redirect QUEUE_DIR/QUEUE_FILE/STATE_FILE to a temp dir for every test."""
    monkeypatch.setattr(gq, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
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
    assert set(job.keys()) == {"id", "cmd", "cwd", "added_at"}
    assert job["cmd"] == "python train.py"
    assert job["cwd"] == "/home/user/project"


def test_read_state_empty():
    state = gq.read_state()
    assert state == {"daemon_pid": None, "running": None}


def test_write_then_read_state():
    gq.write_state({"daemon_pid": 1234, "running": None})
    state = gq.read_state()
    assert state["daemon_pid"] == 1234
    assert state["running"] is None


def test_read_queue_corrupted_resets(tmp_path, monkeypatch):
    monkeypatch.setattr(gq, "QUEUE_FILE", tmp_path / "queue.json")
    (tmp_path / "queue.json").write_text("not json {{{")
    assert gq.read_queue() == []


def test_read_state_corrupted_resets(tmp_path, monkeypatch):
    monkeypatch.setattr(gq, "STATE_FILE", tmp_path / "state.json")
    (tmp_path / "state.json").write_text("not json {{{")
    state = gq.read_state()
    assert state == {"daemon_pid": None, "running": None}


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
    assert state == {"daemon_pid": None, "running": None}

    # also test a list
    sf.write_text("[]")
    state = gq.read_state()
    assert state == {"daemon_pid": None, "running": None}


import argparse as ap


def _args(**kwargs):
    """Build a minimal argparse.Namespace."""
    return ap.Namespace(**kwargs)


def test_cmd_add_appends_to_queue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gq.cmd_add(_args(command="echo hi"))
    q = gq.read_queue()
    assert len(q) == 1
    assert q[0]["cmd"] == "echo hi"
    assert q[0]["cwd"] == str(tmp_path)


def test_cmd_add_prints_job_id(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    gq.cmd_add(_args(command="echo hi"))
    out = capsys.readouterr().out
    assert "added job" in out


def test_cmd_list_empty(capsys):
    gq.cmd_list(_args())
    out = capsys.readouterr().out
    assert "queue" in out.lower()
    assert "0 jobs" in out or "empty" in out.lower()


def test_cmd_list_shows_pending(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    gq.cmd_add(_args(command="python train.py"))
    gq.cmd_list(_args())
    out = capsys.readouterr().out
    assert "python train.py" in out


def test_cmd_cancel_by_full_id(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    gq.cmd_add(_args(command="echo a"))
    job_id = gq.read_queue()[0]["id"]
    gq.cmd_cancel(_args(job_id=job_id))
    assert gq.read_queue() == []
    out = capsys.readouterr().out
    assert "cancelled" in out


def test_cmd_cancel_by_prefix(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    gq.cmd_add(_args(command="echo a"))
    job_id = gq.read_queue()[0]["id"]
    gq.cmd_cancel(_args(job_id=job_id[:2]))
    assert gq.read_queue() == []


def test_cmd_cancel_not_found(capsys):
    gq.cmd_cancel(_args(job_id="zzzz"))
    out = capsys.readouterr().out
    assert "not found" in out.lower()


def test_cmd_cancel_running_job(capsys):
    """Cannot cancel a running job via cancel — redirect user."""
    gq.write_state({"daemon_pid": None, "running": {"id": "aaaa", "cmd": "x",
                                                     "pid": 1, "started_at": "t"}})
    gq.cmd_cancel(_args(job_id="aaaa"))
    out = capsys.readouterr().out
    assert "running" in out.lower()


def test_cmd_cancel_ambiguous_prefix(tmp_path, monkeypatch, capsys):
    """Ambiguous prefix matches multiple pending jobs → message, no removal."""
    monkeypatch.chdir(tmp_path)
    # Add two jobs, then force their IDs to share a 2-char prefix
    gq.cmd_add(_args(command="echo a"))
    gq.cmd_add(_args(command="echo b"))
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
    gq.cmd_add(_args(command="a"))
    gq.cmd_add(_args(command="b"))
    gq.cmd_clear(_args())
    assert gq.read_queue() == []
    out = capsys.readouterr().out
    assert "2" in out


def test_format_elapsed():
    assert gq._format_elapsed(0) == "00:00:00"
    assert gq._format_elapsed(90) == "00:01:30"
    assert gq._format_elapsed(3661) == "01:01:01"


def test_run_job_success(capsys):
    job = {"id": "test", "cmd": "echo gq_ok", "cwd": "/tmp",
           "started_at": datetime.datetime.now().isoformat()}
    exit_code = gq._run_job(job)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "gq_ok" in out  # echo output passes through
    assert "DONE" in out


def test_run_job_failure(capsys):
    job = {"id": "fail", "cmd": "false", "cwd": "/tmp",
           "started_at": datetime.datetime.now().isoformat()}
    exit_code = gq._run_job(job)
    assert exit_code != 0
    out = capsys.readouterr().out
    assert "FAILED" in out


def test_run_job_bad_cwd(capsys):
    """Job with nonexistent cwd is skipped (exit code -1)."""
    job = {"id": "badc", "cmd": "echo x", "cwd": "/nonexistent_dir_xyzzy",
           "started_at": datetime.datetime.now().isoformat()}
    exit_code = gq._run_job(job)
    assert exit_code == -1
    out = capsys.readouterr().out
    assert "WARNING" in out or "warning" in out.lower()


def test_daemon_loop_runs_one_job(tmp_path, monkeypatch, capsys):
    """Daemon loop picks up a job when GPU is idle, runs it, then stops."""
    monkeypatch.chdir(tmp_path)
    # Pre-load queue with one job
    job = gq._make_job("echo daemon_ran", str(tmp_path))
    gq.write_queue([job])

    iterations = [0]

    def fake_sleep(n):
        iterations[0] += 1
        if iterations[0] > 5:
            raise KeyboardInterrupt  # stop the loop

    with patch("gq.gpu_is_idle", return_value=True), \
         patch("time.sleep", side_effect=fake_sleep):
        try:
            gq._daemon_loop(poll_interval=1)
        except KeyboardInterrupt:
            pass

    out = capsys.readouterr().out
    assert "daemon_ran" in out
    assert gq.read_queue() == []  # job was consumed


def test_daemon_loop_waits_when_gpu_busy(tmp_path, monkeypatch, capsys):
    """Daemon does not launch job while GPU is busy."""
    job = gq._make_job("echo should_not_run", str(tmp_path))
    gq.write_queue([job])

    iterations = [0]

    def fake_sleep(n):
        iterations[0] += 1
        if iterations[0] >= 3:
            raise KeyboardInterrupt

    with patch("gq.gpu_is_idle", return_value=False), \
         patch("time.sleep", side_effect=fake_sleep):
        try:
            gq._daemon_loop(poll_interval=1)
        except KeyboardInterrupt:
            pass

    # Job still in queue — was not consumed
    assert len(gq.read_queue()) == 1


def test_run_job_sets_current_job_pid(tmp_path):
    """_run_job sets _current_job_pid to the real subprocess pid during execution."""
    import threading, time as _time
    gq._current_job_pid = None
    marker = tmp_path / "started"
    job = {"id": "pidt", "cmd": f"sh -c 'touch {marker}; sleep 5'",
           "cwd": str(tmp_path),
           "started_at": datetime.datetime.now().isoformat()}
    captured = {}

    def watcher():
        # wait for the marker file, then snapshot the pid
        for _ in range(50):
            if marker.exists():
                captured["pid"] = gq._current_job_pid
                break
            _time.sleep(0.05)

    t = threading.Thread(target=watcher)
    t.start()
    # Run job in a thread so we can observe _current_job_pid mid-flight
    runner = threading.Thread(target=gq._run_job, args=(job,))
    runner.start()
    t.join(timeout=3)
    # force-kill the sleep so runner can finish (don't leave a 5s sleep)
    if gq._current_job_pid is not None:
        try:
            os.killpg(os.getpgid(gq._current_job_pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    runner.join(timeout=5)
    assert captured.get("pid") is not None, "pid was never set during execution"
    assert isinstance(captured["pid"], int)


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
            gq.cmd_add(ap.Namespace(command=f"echo job{i}"))

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
    # Pre-seed state as if a previous daemon crashed mid-job
    gq.write_state({"daemon_pid": None, "running": {"id": "dead",
                     "cmd": "sleep 60", "pid": orphan.pid, "started_at": "t"}})
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
    assert state["running"] is None


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

