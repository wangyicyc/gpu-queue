# Embedded Bash Dialog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the full-screen `_run_bash_for_command` with `_run_embedded_bash`: a real bash (via pty) rendered INSIDE a gq curses dialog to the right of the `Add job` row. pyte parses bash's ANSI output into a screen buffer gq renders cell-by-cell. F5 submits, Esc cancels, Ctrl-C forwards to bash.

**Architecture:** gq opens a pty, spawns `bash --rcfile <rcfile> -i` on the slave (rcfile sources `~/.bashrc` + defines `__gq_capture` + binds F5, reused from current code). A `pyte.Screen` + `pyte.Stream` parse bash's stdout into a screen buffer. A modal loop non-blocking-reads the pty, feeds pyte, renders the buffer into a bordered dialog region (60% h, 70% w), and forwards keystrokes to the pty (F5/Esc/Ctrl-C handled specially). pyte is a lazy import (only the add path).

**Tech Stack:** Python 3 stdlib (`os`, `pty`, `select`, `fcntl`, `termios`, `curses`, `subprocess`, `tempfile`); `pyte` (third-party, lazy-imported). pytest (with `PYTHONPATH=` prefix on this machine).

## Global Constraints

- **pyte is a lazy import** — only inside the embedded-bash code path. `gq watch`/CLI/daemon must NOT import pyte and must work without it installed. If pyte is missing, Add prints `[gq] TUI add needs pyte: pip install pyte` and returns False (no crash, no terminal breakage).
- **Additive** — existing 87 pytest + 8 bash completion must stay green. The only replaced function is `_run_bash_for_command` -> `_run_embedded_bash` (its handoff test is updated). `_tui_do_action`'s add branch changes to call the new function. Everything else (daemon, multi-GPU, env-capture, other TUI actions) unchanged.
- **F5/Esc/Ctrl-C semantics**: F5 submits the current command line (reads cmd/cwd/env from temp files written by the `__gq_capture` rcfile bind, same as today). Esc cancels (closes dialog, no submit). Ctrl-C is forwarded to bash as `\x03` (interrupt test-runs), NOT delivered to gq, while the dialog is active.
- **Teardown on every path**: pty master fd closed, bash process reaped (SIGKILL on Esc/exception), temp files unlinked, curses state restored. No zombie bash, no broken terminal.
- **Tests run with `PYTHONPATH=` prefix** on this machine (ROS pytest plugin conflict — see memory `ros-pytest-workaround`).
- **Current branch:** `main` — branch to `feat/embedded-bash` at execution time.
- The daemon, concurrent loop, multi-GPU scheduling, env-capture, `_launch_job`, log files, and all non-add TUI actions are UNTOUCHED.

---

## File Structure

- **Modify:** `gq` — extract rcfile-building into `_build_capture_rcfile(cmd_path, cwd_path, env_path)` (reused); add `_render_pyte_to_dialog`, `_key_to_bytes`, `_run_embedded_bash`; replace the add branch in `_tui_do_action` to call `_run_embedded_bash`. Remove old `_run_bash_for_command` (or keep as a thin wrapper if any test still calls it — prefer removing and updating the test).
- **Modify:** `tests/test_gq.py` — update the F5-capture test to the new function; add tests for `_render_pyte_to_dialog`, `_key_to_bytes`, the pyte-missing path, teardown.
- **Modify:** `README.md` — document the embedded bash dialog + the `pyte` dependency (install section + TUI section).
- **No new files.**

---

## Task 1: Extract `_build_capture_rcfile` helper (reuse the F5 bind logic)

**Files:**
- Modify: `gq` — extract rcfile-building from `_run_bash_for_command` (gq:1004-1046) into a pure helper `_build_capture_rcfile(cmd_path, cwd_path, env_path) -> str`
- Test: `tests/test_gq.py`

**Interfaces:**
- Produces: `_build_capture_rcfile(cmd_path, cwd_path, env_path) -> str` — returns the rcfile content string (sources ~/.bashrc, defines `__gq_capture` writing the three temp files, binds F5 to it). Pure function (no I/O). Task 4's `_run_embedded_bash` calls it to build the rcfile before spawning bash.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gq.py`:

```python
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
    assert "READLINE_LINE" in rc
    assert "env -0" in rc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH= python -m pytest tests/test_gq.py -k build_capture_rcfile -v`
Expected: FAIL — `module 'gq' has no attribute '_build_capture_rcfile'`.

- [ ] **Step 3: Implement — extract the helper**

In `gq`, add (near `_run_bash_for_command`, before it):

```python
def _build_capture_rcfile(cmd_path: str, cwd_path: str, env_path: str) -> str:
    """Build the bash rcfile that lets F5 capture the current command line.

    Sources ~/.bashrc (so conda/aliases work), defines __gq_capture (writes
    $READLINE_LINE, $PWD, env -0 to the three temp files without executing
    the command), and binds F5 (\\e[15~) to call it. Returns the rcfile
    content string. Pure (no I/O).
    """
    return (
        "[ -f ~/.bashrc ] && source ~/.bashrc || true\n"
        "__gq_capture() {\n"
        f"  printf '%s' \"$READLINE_LINE\" > {cmd_path}\n"
        f"  pwd > {cwd_path}\n"
        f"  env -0 > {env_path}\n"
        "  exit 0\n"
        "}\n"
        'bind -x \'"\e[15~": "__gq_capture"\'\n'
        'echo "[gq] compose your command, then press F5 to submit (Esc to cancel)."\n'
    )
```

Then refactor `_run_bash_for_command` to CALL it: replace the inline rcfile string with `rc_content = _build_capture_rcfile(cmd_path, cwd_path, env_path)` and write `rc_content` to the NamedTemporaryFile. Keep `_run_bash_for_command` working (it's still called by the add branch until Task 4) — just dedupe the rcfile logic.

- [ ] **Step 4: Run the test + full suite**

Run: `PYTHONPATH= python -m pytest tests/test_gq.py -k build_capture_rcfile -v` -> PASS.
Run: `PYTHONPATH= python -m pytest tests/ -q` -> all pass (87; the existing `test_run_bash_for_command_reads_temp_files` still passes because `_run_bash_for_command` still works, now via the helper).

- [ ] **Step 5: Commit**

```bash
git add gq tests/test_gq.py
git commit -m "refactor(gq): extract _build_capture_rcfile helper for reuse"
```

---

## Task 2: `_render_pyte_to_dialog` + `_key_to_bytes` (pure helpers)

**Files:**
- Modify: `gq` — add `_render_pyte_to_dialog(stdscr, screen, oy, ox, h, w)` and `_key_to_bytes(ch) -> bytes | None`
- Test: `tests/test_gq.py`

**Interfaces:**
- Produces: `_render_pyte_to_dialog(stdscr, screen, oy, ox, dh, dw)` renders a `pyte.Screen`-like object's buffer into curses at origin (oy, ox) within a dh x dw region. `_key_to_bytes(ch)` maps a curses key code to the bytes bash/readline expect (KEY_UP -> `\x1b[A`, Ctrl-C -> `\x03`, printable -> the byte), or None for keys gq handles itself (F5/Esc).

- [ ] **Step 1: Write failing tests**

Append to `tests/test_gq.py`:

```python
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
    """A fake pyte screen with known chars -> the right curses writes."""
    import curses as _c
    # Fake a pyte.Screen: .buffer is dict[(y,x)] -> Char(data, fg, bg, bold...)
    # pyte.Char is a namedtuple; build a minimal stand-in.
    Char = type("Char", (), {})
    c = Char(); c.data = "A"; c.fg = "default"; c.bg = "default"; c.bold = False
    screen = type("S", (), {})()
    screen.buffer = {(0, 0): c}
    screen.cursor = type("Cur", (), {})(); screen.cursor.x = 1; screen.cursor.y = 0
    screen.columns = 10; screen.lines = 1

    class MockStd:
        def __init__(self): self.writes = []
        def addstr(self, *a):
            if len(a) == 3: y, x, t = a
            else: y, x, t = a[0], a[1], a[2]
            self.writes.append((y, x, t))
        def move(self, *a): pass
        def refresh(self): pass
    std = MockStd()
    gq._render_pyte_to_dialog(std, screen, oy=2, ox=5, dh=1, dw=10)
    # The 'A' at screen (0,0) should be drawn at curses (oy+0, ox+0) = (2,5).
    assert (2, 5, "A") in std.writes or any(w[0] == 2 and w[1] == 5 and "A" in w[2] for w in std.writes)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH= python -m pytest tests/test_gq.py -k "key_to_bytes or render_pyte_to_dialog" -v`
Expected: FAIL — no `_key_to_bytes` / `_render_pyte_to_dialog`.

- [ ] **Step 3: Implement `_key_to_bytes`**

In `gq` (in the TUI section):

```python
def _key_to_bytes(ch: int):
    """Map a curses key code to the bytes bash/readline expect, or None if
    the key is reserved for gq (F5 submit, Esc cancel)."""
    import curses as _c
    reserved = {_c.KEY_F5, 27}  # F5, Esc — gq handles these
    if ch in reserved:
        return None
    arrow = {_c.KEY_UP: b"\x1b[A", _c.KEY_DOWN: b"\x1b[B",
             _c.KEY_RIGHT: b"\x1b[C", _c.KEY_LEFT: b"\x1b[D"}
    if ch in arrow:
        return arrow[ch]
    if ch == _c.KEY_ENTER:
        return b"\r"
    if ch == _c.KEY_BACKSPACE or ch == 127 or ch == 8:
        return b"\x7f"
    if 0 <= ch < 256:
        return bytes([ch])   # printable + Ctrl combos (Ctrl-C=3, Ctrl-D=4, ...)
    return None
```

- [ ] **Step 4: Implement `_render_pyte_to_dialog`**

```python
def _render_pyte_to_dialog(stdscr, screen, oy, ox, dh, dw):
    """Render a pyte.Screen's buffer into curses at origin (oy, ox) within a
    dh x dw region. Draws each non-empty cell; maps pyte fg/bg/bold to curses
    color pairs + A_BOLD (best-effort — default colors map to A_NORMAL).
    """
    import curses as _c
    buffer = screen.buffer
    for y in range(dh):
        # Build the line string + track the last non-empty cell.
        cells = []
        for x in range(dw):
            ch = buffer.get((y, x))
            data = ch.data if ch is not None and ch.data != " " else " "
            cells.append(data)
        line = "".join(cells).rstrip()
        # Pad to dw so old chars are cleared.
        line = line[:dw].ljust(dw)
        try:
            stdscr.addstr(oy + y, ox, line, _c.A_NORMAL)
        except _c.error:
            pass
    # Move the cursor to pyte's cursor position (clamped to the region).
    cx = min(getattr(screen.cursor, "x", 0), dw - 1)
    cy = min(getattr(screen.cursor, "y", 0), dh - 1)
    try:
        stdscr.move(oy + cy, ox + cx)
    except _c.error:
        pass
```

NOTE for the implementer: this is a simplified render (monochrome, no per-cell color). The spec mentions color mapping, but a working monochrome render is acceptable for v1 — color can be a follow-up. If pyte's `buffer` access differs across pyte versions (it's a dict in pyte 0.x), the test's fake screen matches the dict shape. If the real pyte Screen API differs, adjust the render to read `screen.display` (a list of line strings, simpler) instead of `buffer` — `screen.display` is available after `stream.feed(...)`. Prefer `screen.display` for simplicity:

```python
def _render_pyte_to_dialog(stdscr, screen, oy, ox, dh, dw):
    import curses as _c
    display = screen.display  # list of strings, one per line
    for y in range(dh):
        line = (display[y] if y < len(display) else "")[:dw].ljust(dw)
        try:
            stdscr.addstr(oy + y, ox, line, _c.A_NORMAL)
        except _c.error:
            pass
    cx = min(getattr(screen.cursor, "x", 0), dw - 1)
    cy = min(getattr(screen.cursor, "y", 0), dh - 1)
    try:
        stdscr.move(oy + cy, ox + cx)
    except _c.error:
        pass
```

Use the `screen.display` form. Update the test's fake screen to have a `display` attribute (list `["A"]`) instead of/in addition to `buffer`, so the test matches.

- [ ] **Step 5: Run tests + full suite**

Run: `PYTHONPATH= python -m pytest tests/test_gq.py -k "key_to_bytes or render_pyte_to_dialog" -v` -> PASS (adjust the render test to use `display`).
Run: `PYTHONPATH= python -m pytest tests/ -q` -> all pass (87 + 3 new = 90).

- [ ] **Step 6: Commit**

```bash
git add gq tests/test_gq.py
git commit -m "feat(gq): _render_pyte_to_dialog + _key_to_bytes helpers for embedded bash"
```

---

## Task 3: `_run_embedded_bash` modal loop (the core)

**Files:**
- Modify: `gq` — add `_run_embedded_bash(stdscr, oy, ox) -> dict | None` (the pty + pyte + modal loop). Do NOT wire into `_tui_do_action` yet (Task 4 does).
- Test: `tests/test_gq.py`

**Interfaces:**
- Consumes: `_build_capture_rcfile` (Task 1), `_render_pyte_to_dialog` + `_key_to_bytes` (Task 2).
- Produces: `_run_embedded_bash(stdscr, oy, ox) -> dict | None`. Spawns bash in a pty, renders its output via pyte into a dialog at (oy, ox), forwards keys (F5 submit / Esc cancel / Ctrl-C to bash). Returns `{"cmd","cwd","env"}` on F5, None on Esc. Raises nothing — cleans up on every path.

- [ ] **Step 1: Implement `_run_embedded_bash`**

Add to `gq` (lazy `import pyte` inside; uses `pty`, `select`, `os`, `fcntl`, `termios`, `struct`, `tempfile`, `signal`):

```python
def _run_embedded_bash(stdscr, oy, ox):
    """Spawn a real bash in a pty, rendered inside a curses dialog at (oy, ox).
    F5 submits the current command line (via the rcfile __gq_capture bind);
    Esc cancels; Ctrl-C is forwarded to bash. Returns {"cmd","cwd","env"} or
    None. Lazy-imports pyte; if pyte is missing, prints an install hint and
    returns None.
    """
    import curses as _c
    try:
        import pyte
    except ImportError:
        print("\n[gq] TUI add needs pyte: pip install pyte", file=sys.stderr)
        return None

    h, w = stdscr.getmaxyx()
    dh = max(8, int(h * 0.6))
    dw = max(40, int(w * 0.7))
    # Clamp so the dialog fits.
    dh = min(dh, h - oy - 1)
    dw = min(dw, w - ox - 1)

    import tempfile, os, pty, select, fcntl, termios, struct, signal
    cmd_fd, cmd_path = tempfile.mkstemp(prefix="gq_cmd_")
    cwd_fd, cwd_path = tempfile.mkstemp(prefix="gq_cwd_")
    env_fd, env_path = tempfile.mkstemp(prefix="gq_env_")
    rc_fd, rc_path = tempfile.mkstemp(prefix="gq_rc_")
    for fd in (cmd_fd, cwd_fd, env_fd, rc_fd):
        os.close(fd)
    os.write(open(rc_path, "w").fileno() if False else rc_fd,
             _build_capture_rcfile(cmd_path, cwd_path, env_path).encode()) if False else None
    # Write rcfile content (the above no-op guard is to avoid double-open;
    # do it simply:
    with open(rc_path, "w") as f:
        f.write(_build_capture_rcfile(cmd_path, cwd_path, env_path))

    screen = pyte.Screen(dw, dh)
    stream = pyte.Stream(screen)

    master, slave = pty.openpty()
    # Set the pty window size so bash knows the dialog dimensions.
    winsize = struct.pack("HHHH", dh, dw, 0, 0)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize)

    import subprocess
    proc = subprocess.Popen(["bash", "--rcfile", rc_path, "-i"],
                            stdin=slave, stdout=slave, stderr=slave,
                            preexec_fn=os.setsid, env={**os.environ})
    os.close(slave)

    # Override SIGINT so Ctrl-C is NOT delivered to gq; we forward \x03 to bash.
    old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    _tui_blocking_mode(stdscr)  # blocking getch for the modal loop
    result = None
    try:
        while True:
            # 1. Non-blocking read bash output, feed pyte.
            while True:
                r, _, _ = select.select([master], [], [], 0)
                if not r:
                    break
                try:
                    data = os.read(master, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                stream.feed(data.decode("utf-8", "replace"))
            # 2. Render the dialog.
            stdscr.erase()  # NOTE: caller re-renders the panel after; here draw dialog+border
            # Draw border + title.
            try:
                stdscr.box()  # simple full-screen box; for a sub-box use stdscr.subwin
            except _c.error:
                pass
            _render_pyte_to_dialog(stdscr, screen, oy, ox, dh, dw)
            stdscr.addstr(h - 1, 0, " F5 submit  Esc cancel  Ctrl-C interrupts test-run "[:w-1],
                          _c.color_pair(4))
            stdscr.refresh()

            # 3. Get a key (blocking, ~100ms timeout to re-read bash output).
            _c.halfdelay(10)  # 1s tick
            ch = stdscr.getch()
            if ch == _c.KEY_F5:
                # __gq_capture wrote the temp files; read them.
                cmd = Path(cmd_path).read_text()
                if cmd:
                    cwd = Path(cwd_path).read_text().strip()
                    env = {}
                    raw = Path(env_path).read_bytes()
                    for entry in raw.split(b"\x00"):
                        if b"=" in entry:
                            k, _, v = entry.partition(b"=")
                            try:
                                env[k.decode()] = v.decode()
                            except UnicodeDecodeError:
                                continue
                    result = {"cmd": cmd, "cwd": cwd, "env": env}
                break
            if ch == 27:  # Esc
                break
            kb = _key_to_bytes(ch)
            if kb is not None:
                try:
                    os.write(master, kb)
                except OSError:
                    break
            # If bash exited (proc.poll() not None and no more output), break.
            if proc.poll() is not None:
                # Drain remaining output then break.
                try:
                    data = os.read(master, 4096)
                    if data:
                        stream.feed(data.decode("utf-8", "replace"))
                except OSError:
                    pass
                break
    finally:
        _tui_restore_halfdelay(stdscr)
        signal.signal(signal.SIGINT, old_sigint)
        try:
            os.close(master)
        except OSError:
            pass
        try:
            proc.kill()
            proc.wait()
        except Exception:
            pass
        for p in (cmd_path, cwd_path, env_path, rc_path):
            try:
                os.unlink(p)
            except OSError:
                pass
    return result
```

NOTE for the implementer: the above is a starting skeleton — several details need care during implementation:
- The `stdscr.erase()` + `stdscr.box()` redraws the WHOLE screen with a box; this clobbers the main panel. The caller (Task 4) re-renders the panel after the dialog closes, but DURING the dialog the main panel is gone. Acceptable (Add is modal, the dialog owns the screen). But `stdscr.box()` draws a border around the whole stdscr, not the dialog region — for a bordered dialog at (oy,ox), use `stdscr.subwin(dh, dw, oy, ox)` and box+render into the subwindow. Adjust: create `win = stdscr.subwin(dh, dw, oy, ox); win.box(); win.addstr(...); _render_pyte_to_dialog(win, screen, 1, 1, dh-2, dw-2)` (inset by border). Use the subwin form.
- `halfdelay(10)` = 1s; that's fine but means up to 1s latency re-reading bash output after a key. Acceptable. Could lower to `halfdelay(2)` (0.2s) for snappier bash output streaming.
- The bash-exit detection (`proc.poll()`) handles Ctrl-D in bash (bash exits, dialog closes).
- Test scaffolding: mock `pty.openpty`, `subprocess.Popen`, `select.select`, `os.read` to simulate bash writing the F5 temp files. This is involved; see Step 2's test.

- [ ] **Step 2: Write a test for the F5 submit path (mocked)**

Append to `tests/test_gq.py`:

```python
def test_run_embedded_bash_f5_submit(monkeypatch, tmp_path):
    """F5 in the embedded bash reads the captured temp files and returns the dict."""
    import importlib.util, importlib.machinery
    # Lazy pyte: only skip if truly unavailable — but the function handles ImportError.
    # Build fake temp files as if __gq_capture fired.
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
    monkeypatch.setattr(_os, "write", lambda fd, data: len(data))
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

    class MockWin:
        def __init__(self): self.k = [gq.curses.KEY_F5 if hasattr(gq,'curses') else None]
        def getmaxyx(self): return (24, 80)
        def box(self): pass
        def addstr(self, *a, **kw): pass
        def refresh(self): pass
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

    result = gq._run_embedded_bash(MockWin(), oy=2, ox=0)
    assert result is not None
    assert result["cmd"] == "torchrun --nproc_per_node=4 train.py"
    assert result["cwd"] == "/home/walle/proj"
    assert result["env"]["MY"] == "1"
```

NOTE: this test is intricate (many mocks). If pyte isn't installed in the test env, `_run_embedded_bash` returns None immediately (ImportError) — so the test requires pyte installed. Skip the test gracefully if pyte is missing: wrap with `pytest.importorskip("pyte")` at the top of the test.

- [ ] **Step 3: Run the test + full suite**

Run: `PYTHONPATH= python -m pytest tests/test_gq.py -k run_embedded_bash_f5 -v` -> PASS (if pyte installed; else skip).
Run: `PYTHONPATH= python -m pytest tests/ -q` -> all pass.

- [ ] **Step 4: Commit**

```bash
git add gq tests/test_gq.py
git commit -m "feat(gq): _run_embedded_bash modal loop (pty + pyte + key forwarding)"
```

---

## Task 4: Wire `_run_embedded_bash` into the add action + remove old `_run_bash_for_command`

**Files:**
- Modify: `gq` — `_tui_do_action` add branch calls `_run_embedded_bash`; remove `_run_bash_for_command` (or keep if referenced). Update `_tui_do_action` to compute the dialog origin (the Add row's screen y + label width).
- Test: `tests/test_gq.py` — update `test_run_bash_for_command_reads_temp_files` (the old function is gone) to test `_run_embedded_bash` instead (already done in Task 3 Step 2; remove/replace the old test).

- [ ] **Step 1: Wire the add branch**

In `_tui_do_action` (the add branch), replace:
```python
        captured = _run_bash_for_command()
```
with:
```python
        # Dialog origin: the Add row's screen y, just after the "Add job" label.
        focus_y = 1 + _total_cards() + 1 + focus
        captured = _run_embedded_bash(stdscr, oy=focus_y, ox=len("Add job") + 2)
```
(Reuse the `focus_y` already computed at the top of `_tui_do_action`.)

- [ ] **Step 2: Remove `_run_bash_for_command`**

Delete the old `_run_bash_for_command` function (its rcfile logic is now `_build_capture_rcfile`, its subprocess logic is in `_run_embedded_bash`). Grep to confirm nothing else calls it: `grep _run_bash_for_command gq tests/test_gq.py` — only the old test should reference it; update/delete that test.

- [ ] **Step 3: Update the old test**

`test_run_bash_for_command_reads_temp_files` tested the old function. Delete it (Task 3 Step 2's `test_run_embedded_bash_f5_submit` covers the new function's handoff). Or adapt it to call `_run_embedded_bash` if the mocks transfer cleanly.

- [ ] **Step 4: Run the full suite + completion**

Run: `PYTHONPATH= python -m pytest tests/ -q` -> all pass (count adjusted; old test removed, new test present).
Run: `bash tests/test_completion.sh` -> 8 passed (no change).

- [ ] **Step 5: Manual E2E (the critical verification)**

Install pyte if not present: `pip install pyte`. Sync `~/.local/bin/gq`. Run `gq` (no args) in a real terminal, select Add, verify:
1. A bordered dialog appears to the right of `Add job` with a bash prompt inside.
2. Typing works; `ls` shows output; tab completes; `cd` works.
3. F5 submits the current command line -> dialog closes -> --gpus select -> job enqueued (gq list confirms, with the right cmd/cwd/env).
4. Esc cancels (dialog closes, no job added).
5. Ctrl-C in the dialog interrupts a test-run command (e.g. `python -c "import time; time.sleep(60)"`), not gq.
6. Resize: dialog redraws without breaking.
7. Terminal is restored after the dialog closes (no curses breakage).

If a real terminal isn't available in the harness, drive via a pty (Task 3/4 implementers did this before). Document the result.

- [ ] **Step 6: Commit**

```bash
git add gq tests/test_gq.py
git commit -m "feat(gq): add uses embedded bash dialog; remove full-screen _run_bash_for_command"
```

---

## Task 5: README — document embedded bash + pyte dependency

**Files:**
- Modify: `README.md` — 中文 + English: update the TUI section (Add -> embedded bash dialog, F5/Esc/Ctrl-C); add `pip install pyte` to install (only for TUI add); note watch/CLI work without pyte.

- [ ] **Step 1: Update 中文 TUI section + install**

In the 中文 `### TUI 可视化面板` section, change the Add bullet to describe the embedded dialog (Add -> 右侧展开 bash 对话框,F5 提交,Esc 取消,Ctrl-C 中断试跑). In `### 安装`, add a note: "TUI 的 Add 功能需要 pyte:`pip install pyte`(watch/命令行不需要)".

- [ ] **Step 2: Update English TUI section + install**

Mirror in the English `### TUI panel` section and `### Install`.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document embedded bash dialog and pyte dependency"
```

---

## Self-Review

**1. Spec coverage:**
- "Embedded bash via pty + pyte" -> Task 3. ✓
- "Dialog to the right of Add row, 60% h, 70% w" -> Task 3 (dh/dw computation) + Task 4 (origin = Add row y + label width). ✓
- "F5 submit (reuse rcfile bind)" -> Task 1 (_build_capture_rcfile) + Task 3 (reads temp files on F5). ✓
- "Esc cancel" -> Task 3. ✓
- "Ctrl-C forwarded to bash" -> Task 2 (_key_to_bytes maps 3 -> \x03) + Task 3 (SIG_IGN during modal + forward \x03). ✓
- "pyte lazy import + friendly error" -> Task 3 (ImportError -> print + return None). ✓
- "Teardown on every path" -> Task 3 finally block. ✓
- "watch/CLI/daemon unchanged, no pyte import" -> lazy import only in _run_embedded_bash; global constraint. ✓
- "README" -> Task 5. ✓
- "Existing tests stay green" -> every task runs full suite. ✓

**2. Placeholder scan:** No TBD/TODO. All code blocks complete. Task 3's skeleton has "NOTE for the implementer" guidance on subwin/halfdelay — that's direction, not a placeholder (the corrected subwin form is given). Task 3's test is intricate but complete (with `pytest.importorskip("pyte")` noted).

**3. Type consistency:** `_build_capture_rcfile(cmd_path, cwd_path, env_path) -> str` — consistent in Task 1 (produces) and Task 3 (calls). `_render_pyte_to_dialog(stdscr, screen, oy, ox, dh, dw)` — consistent in Task 2 (produces) and Task 3 (calls, with subwin). `_key_to_bytes(ch) -> bytes | None` — consistent in Task 2 (produces) and Task 3 (calls). `_run_embedded_bash(stdscr, oy, ox) -> dict | None` — consistent in Task 3 (produces) and Task 4 (calls). The return dict `{"cmd","cwd","env"}` matches `_tui_do_action`'s add branch (which reads `captured["cmd"]`/`["cwd"]`/`["env"]`).

One gap: Task 3's render uses `screen.display` (list of strings) but Task 2's test fake screen uses `buffer`. The NOTE in Task 2 Step 4 says to prefer `screen.display` and update the test's fake to have `display`. The implementer MUST make Task 2's test fake match the `display` form chosen. Flagged for the implementer.

No other gaps. Plan is complete (with the flagged render-test fake-screen alignment).
