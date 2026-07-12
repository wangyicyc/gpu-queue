# Design: Embedded Bash Dialog for `gq` TUI Add

**Date:** 2026-07-11
**Status:** Approved (pending spec review) — all design decisions confirmed via brainstorming

## Problem

The current `add` flow spawns a real bash that takes over the whole terminal (`subprocess` replaces stdio). The user sees bash's own output, not a gq-rendered UI — gq "disappears" while composing the command. The user wants the bash to live **inside a gq-rendered dialog**: selecting `Add job` expands a bash region to the right of the `Add job` row (like the inline confirm prompt), where a real bash runs (tab-complete, cd, test-run all work), and F5 submits the current command line.

## Researched / verified foundation

- **pty + pyte is the standard way to embed an interactive shell in a curses app.** gq opens a pty (master/slave), spawns bash on the slave side; gq reads bash's output from the master, writes user keystrokes to the master. bash gets a real tty (readline, tab, ANSI all work).
- **bash output is ANSI-laden** (color, cursor moves, readline redraw). A curses `addstr` can't render it directly. `pyte` is a pure-Python terminal emulator: feed it bash's output stream, it maintains a screen buffer (lines + cursor + colors) that gq can render cell-by-cell into curses. This is the robust path (vs writing an ANSI parser by hand, which is error-prone).
- **Cost: adds the `pyte` dependency** (pure Python, plus `wcwidth`). The user accepted this. To minimize impact: `pyte` is imported lazily (only on the TUI add path), so `gq watch`/CLI/daemon work without pyte installed; only `gq` (TUI) → Add needs it.

## Design — all decisions confirmed

### Architecture

1. **Add spawns an embedded bash in a gq-rendered dialog**, not a full-screen subprocess. The dialog appears to the RIGHT of the `Add job` row, expanding downward ~60% of terminal height, ~70% width.
2. **Real bash via pty.** gq opens a pty, spawns `bash --rcfile <rcfile> -i` on the slave (rcfile sources `~/.bashrc` + defines `__gq_capture` + binds F5, same as today). bash inherits gq's env (conda env works).
3. **pyte renders bash output.** gq reads bash's stdout (from pty master) in a non-blocking loop, feeds it to a `pyte.Screen` (with a `Stream` to parse ANSI), and renders the screen's buffer into the curses dialog region each frame.
4. **Keystrokes pass through to bash**, except two reserved keys: **F5** submits the current command line (read from pyte's screen — the line the cursor is on, after the prompt), **Esc** cancels (closes the dialog, no submit). While the bash dialog is active, **Ctrl-C is forwarded to bash** (so test-run commands can be interrupted), not to gq.
5. **F5 capture**: the rcfile's `__gq_capture` still writes `$READLINE_LINE` + `pwd` + `env -0` to temp files (existing mechanism, proven in Task 4). gq reads them on F5. (Alternatively gq could read the command from pyte's screen, but reusing the F5 bind is proven and handles the edge cases — stick with it.)
6. **After F5**: dialog closes → `_tui_select_gpus` (existing) → `_make_job` with captured cmd/cwd/env (READLINE stripped, existing) → enqueue. Same post-F5 flow as today.
7. **pyte lazy import**: `import pyte` only inside the add dialog code. If pyte is missing, the Add action prints `[gq] TUI add needs pyte: pip install pyte` and returns False (no crash, no terminal breakage). `gq watch`/CLI/daemon never import pyte.

### Dialog layout

```
┌─ gq ─── 1 GPUs ───────── 14:32 ─┐
│ GPU 0  ████  ab12  4:12         │
│ ───────────────────────────────│
│ ▸ Add job  ┌───────────────────┐│  ← dialog starts at Add row, right side
│            │ $ ls              ││     height = 60% of terminal
│            │ train.py  eval.py ││     width  = 70% of terminal
│            │ $ torchrun ...█   ││
│            └───────────────────┘│
│   Stop: ab12  (torchrun...)     │  ← other ops pushed down (or hidden if overflow)
│   Quit                          │
│  F5 submit  Esc cancel  (test-run: Ctrl-C) │
└─────────────────────────────────┘
```
- Dialog origin: the `Add job` row's y, x = after the `Add job` label.
- Dialog height: `max(8, int(h * 0.6))`; width: `max(40, int(w * 0.7))`, clamped to terminal.
- Other operation rows: rendered below the dialog (pushed down); if they don't fit, they're hidden until the dialog closes (acceptable — Add is modal).
- Dialog has a border + title `compose (F5 submit, Esc cancel)`.
- Bottom hint updates to `F5 submit  Esc cancel  Ctrl-C interrupts test-run`.

### Interaction loop (modal)

When Add is selected:
1. Open pty, spawn bash (rcfile with bashrc + F5 bind).
2. Create `pyte.Screen(cols, rows)` + `pyte.Stream(screen)`.
3. Modal loop (no halfdelay auto-refresh of the main panel — the dialog owns the screen):
   - Non-blocking read pty master (`os.read` with `select`, short timeout ~50ms); feed bytes to `pyte.Stream`.
   - Render: draw the dialog border + pyte screen buffer into the dialog region (cell-by-cell: char + color from pyte's `buffer`/`cursor`).
   - `stdscr.getch()` (blocking, short timeout): forward printable/arrow/Ctrl keys to pty master (as the key's byte / ANSI sequence). F5 → break+submit. Esc → break+cancel. Ctrl-C → forward `\x03` to pty (bash sends SIGINT to its child). Resize → recompute dialog size + `screen.resize` + `TIOCSWINSZ` on the pty.
4. On F5: read temp files (cmd/cwd/env, written by `__gq_capture`), close pty, tear down dialog, call `_tui_select_gpus` → enqueue.
5. On Esc: close pty (SIGKILL bash), tear down, return to main panel.

### Files

- **`gq`**: replace `_run_bash_for_command` (full-screen subprocess) with `_run_embedded_bash(stdscr, origin_y, origin_x) -> dict | None` (renders the pyte screen into a curses dialog; returns captured cmd/cwd/env or None on Esc). `_tui_do_action`'s add branch calls `_run_embedded_bash` instead. Lazy `import pyte` inside. Keep the rcfile/F5-bind logic (extract from old `_run_bash_for_command`).
- **`tests/test_gq.py`**: test the F5-capture handoff (mock pty + pyte Screen with a pre-filled buffer; assert cmd/cwd/env read). Test the pyte-missing path (mock `import pyte` raising ImportError → Add prints the install message, returns False). Existing tests stay green.
- **`README.md`**: document the embedded bash dialog (Add → bash region right of Add row, F5 submit, Esc cancel, Ctrl-C interrupts test-run), the `pyte` dependency (`pip install pyte`, only for TUI add), and update the install section.

### Key implementation risks (flagged for the plan)

1. **pyte rendering correctness.** Rendering pyte's screen buffer to curses cell-by-cell must handle: colors (pyte's `buffer` is a dict of (y,x)→Char with fg/bg/attrs → map to curses color pairs + attrs), cursor position (draw cursor or hide), and wide chars (wcwidth — pyte handles). The plan must include a focused test of the render mapping.
2. **Key forwarding.** Mapping curses key codes (KEY_UP, KEY_LEFT, Ctrl-C=`3`, etc.) to the byte sequences bash/readline expects (e.g. KEY_UP → `\x1b[A`) must be correct, else arrows/history break. A mapping table is needed.
3. **Non-blocking pty read + responsive getch.** The modal loop must read bash output without blocking (select with short timeout) AND respond to keys immediately. If bash floods output, gq must not freeze. Backpressure: if the read buffer grows, batch-feed pyte.
4. **Ctrl-C forwarding.** curses' default SIGINT handler must be overridden during the modal loop so Ctrl-C (byte `\x03`) is sent to the pty, not delivered to gq. Restore on exit.
5. **Teardown on every path.** pty master fd closed, bash process reaped (SIGKILL on Esc/exception), temp files unlinked, curses state restored. The dialog must not leave a zombie bash or a broken terminal on any exit path.
6. **pyte not installed.** Lazy import + friendly error; must not crash the TUI or break the terminal.

## Testing strategy

1. **pyte render mapping** (pytest): a fake pyte screen with a known buffer → assert `_render_pyte_to_dialog` produces the right (y,x,char,attr) curses writes. Cover color + cursor.
2. **F5 capture handoff** (pytest): mock `os.read` (pty master) to return bash output that includes the F5 bind having fired (temp files written); assert `_run_embedded_bash` returns the captured dict. (Reuses the Task 4 handoff test pattern.)
3. **pyte-missing path** (pytest): monkeypatch `builtins.__import__` to raise ImportError for `pyte`; assert Add prints the install message and returns False without crashing.
4. **Key-forwarding map** (pytest): assert `_key_to_bytes(KEY_UP)` == `b'\x1b[A'`, Ctrl-C → `b'\x03'`, printable `'a'` → `b'a'`, etc.
5. **Teardown** (pytest): after `_run_embedded_bash` returns (submit or cancel), assert pty fd closed, bash process reaped, temp files unlinked.
6. **Manual pty E2E**: drive the TUI via a pty, select Add, type a command in the embedded bash, F5, confirm the job is enqueued with the right cmd/cwd/env. Verify Esc cancels, Ctrl-C interrupts a test-run, resize works.
7. Existing 87 pytest + 8 bash completion stay green (no regressions; `_run_bash_for_command` is replaced but its handoff test is updated to the new function).

## Out of scope

- Embedded bash for anything other than Add (Stop/Cancel/Clear stay select+confirm).
- Mouse support in the dialog.
- Multiple simultaneous bash dialogs.
- Scrollback history in the dialog beyond what pyte's screen holds (pyte maintains a screen buffer of `rows` lines; older output scrolls off — acceptable for a compose dialog).
- A fallback to the old full-screen bash (the pyte-missing path just tells the user to install pyte; no degraded embedded fallback).

## Migration note

After merge: re-sync `~/.local/bin/gq`; users must `pip install pyte` to use TUI Add (documented). `gq watch`/CLI work without pyte. Old `queue.json`/`state.json` unaffected.
