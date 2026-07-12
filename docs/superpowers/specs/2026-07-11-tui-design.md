# Design: TUI Visualization for `gq`

**Date:** 2026-07-11
**Status:** Approved (pending spec review) — all design decisions confirmed via brainstorming

## Problem

`gq` is operated entirely by typing commands (`gq add ...`, `gq stop <id>`, etc.) and its output is plain text. The user wants a ranger/htop-style full-screen TUI: typing bare `gq` opens a panel where operations and jobs are selected with arrow keys + Enter (no typing command names, no hotkey letters to memorize), with htop-quality rendering (colored bars, no-flicker refresh, instant key response).

## Researched / verified foundation

- **htop refresh model**: htop DOES refresh (default ~1.5s), not "no refresh" — the "looks static" feel comes from double-buffered diff updates (`noutrefresh`+`doupdate`, only changed cells redraw) + instant key response via non-blocking `getch`/`halfdelay`. Pure curses can replicate this.
- **bash `bind -x`**: bash lets `bind -x '"\e[15~": "cmd"'` bind F5 to a command that reads `$READLINE_LINE` (the current input line, not yet executed) and can `exit` the shell. This is the mechanism to "grab the current bash command line without executing it". Verified as a standard bash feature (bash 4+). Limitations: bash-only (not zsh/fish); F5's escape sequence (`\e[15~`) is standard xterm but may vary on exotic terminals; the bind must be injected into the spawned bash's startup.
- **curses capabilities**: 256-color, block chars `█░▏▎▍▌▋▊▉`, single-line box `┌─┐│└┘`, `halfdelay`, `noutrefresh`/`doupdate`, `KEY_RESIZE` — all in Python stdlib `curses`. Truecolor and mouse are NOT targeted (out of scope).

## Design — all decisions confirmed

### Architecture

1. **`gq` with no argument = enter TUI panel.** `gq watch` is unchanged (foreground daemon in tmux, prints summary only — task stdout/stderr goes to log files). All CLI subcommands (`add/list/stop/cancel/clear/watch`) remain unchanged; TUI is a new entry point.
2. **TUI is a view; daemon is independent.** TUI reads `state.json`/`queue.json`/`busy_cards()` to render; write-operations (add/stop/cancel/clear) write through the existing state files, which the daemon picks up. TUI can open/close freely without affecting jobs. (Same IPC mechanism already used by `gq add`/`gq stop`.)
3. **Task logs: `~/.gpu-queue/logs/<jobid>.log`.** The daemon redirects each job's stdout/stderr to this file (was: printed to the `gq watch` terminal). The watch terminal now prints only summary lines (`[gq] >>> job X starting`, `<<< DONE/FAILED`), not task output. TUI can display a job's log tail.
4. **Pure curses, zero dependencies.** Single-file `gq` script preserved. curses is stdlib (Linux; Windows-curses out of scope).

### Main panel layout (Layout X: operations list primary)

```
┌─ gq ─────────────────── 8 GPUs ──── 14:32:07 ─┐
│ GPU 0 ████ ab12 4:12   GPU 1 idle   ...        │   ← top GPU status bar (read-only)
│                                                │
│ ▸ Add job                                      │   ← operations list (↑↓ select, Enter run)
│   Stop: ab12  (torchrun... GPU 0,1,2,3)        │
│   Cancel: #1 ef56  (python eval.py)            │
│   Cancel: #2 gh78  (bash run.sh)               │
│   Clear queue                                  │
│   Open log: ab12                               │
│   Quit                                         │
│                                                │
│ ↑↓ select  Enter confirm                       │
└────────────────────────────────────────────────┘
```

- **Top bar**: GPU overview — one compact line per GPU (or a wrapped summary): utilization bar, owning job id, elapsed. Read-only; not selectable.
- **Operations list**: the selectable region. Static ops (`Add job`, `Clear queue`, `Quit`) + dynamic ops derived from current state:
  - one `Stop: <id>` line per running job
  - one `Cancel: #<n> <id>` line per queued job
  - one `Open log: <id>` line per running job (or per job with a log file)
- **Arrow keys + Enter only.** No hotkey letters. ↑↓ move the cursor; Enter activates the focused line. (No mouse.)
- Selection = reverse-video (foreground/background swap), htop-style.

### Operation flows

**Add job** (the one place typing happens — but typing happens inside a real bash, not a curses input box):
1. Select `Add job`, Enter.
2. TUI spawns a **real bash subprocess** that takes over the terminal (like vim `:sh`), with an injected `bind -x` so that **F5** writes the current readline line (`$READLINE_LINE`) to `/tmp/gq_cmd.<pid>.txt`, the current cwd to a second file, then `exit`s the bash. The bash inherits the TUI process's env (so `conda activate` inside works).
3. User freely uses bash: `cd` into the project dir, `ls`, tab-complete, even test-run OTHER commands to verify the env. Then types the command they want queued (`torchrun --nproc_per_node=4 train.py`) and **does NOT press Enter** — instead presses **F5**.
4. F5 grabs: command string (readline line), cwd (bash's `$PWD`), env (bash's exported env at F5 time — captured by having the bind also dump `env` to a file). bash exits, TUI resumes.
5. TUI reads command+cwd+env. Pops a **`--gpus` selector**: lists `1, 2, ..., N` (N = `_total_cards()`), ↑↓ select, Enter.
6. TUI writes the job to `queue.json` (cmd, cwd, env, n) — same shape as `cmd_add` produces. Returns to main panel; the new job appears in the queue.

**Stop / Cancel / Open log / Clear / Quit**: select the line, Enter. Stop/Cancel/Clear confirm before acting (a `y/n` prompt or a `[Confirm] [Cancel]` mini-select). They write through the existing state/queue files exactly as the CLI commands do.

### Refresh & rendering (htop-aligned)

- **`curses.halfdelay(20)`** (2-second refresh tick): `getch` returns every 2s (triggering a re-read + redraw) OR immediately on a keypress. → auto-refresh + instant key response, same as htop.
- **Double-buffer diff**: `noutrefresh()` on each pad/window, `doupdate()` once per cycle. Only changed cells are sent to the terminal → no flicker.
- **Progress bars**: `█` for filled, `░` for empty; half-block chars `▏▎▍▌▋▊▉` for sub-character precision. Color by utilization: green <50%, yellow 50-80%, red >80% (256-color `init_pair`).
- **Selection**: reverse-video the focused line.
- **Borders**: single-line box `┌─┐│└┘`.
- **Resize**: handle `KEY_RESIZE`, recompute layout.
- **Top status line**: total GPUs, running count, queued count, wall-clock time.

### What does NOT change

- The multi-GPU scheduling, concurrent daemon loop, env-capture, `busy_cards`/`_total_cards`, crash recovery — untouched. TUI is a new view on top.
- All CLI subcommands and their behavior — untouched. TUI is additive.
- `gq watch` still starts the daemon; its terminal output changes (summary only, task output → log files) but the command/flags are unchanged.

## Key implementation risks (flagged for the plan)

1. **F5 + bash `bind` mechanism is the riskiest piece.** It's bash-only, depends on F5's escape sequence being `\e[15~` (xterm-standard but not universal), requires injecting bind config into the spawned bash, and grabbing env/cwd/cmd via temp files is a multi-step handoff. The plan must include a fallback path: if F5/bind proves unreliable across the user's terminals, degrade `Add` to a simple curses single-line input box (no cd/tab/test-run, but typed command + gpus select). The fallback is strictly less capable but unblocks the feature.
2. **curses + the existing SIGINT handler / subprocess jobs**: the TUI process is separate from the daemon, so it doesn't manage job signals — it only writes state. But the TUI's own curses session must clean up on Ctrl-C/resize/exit (restore terminal via `curses.endwin`), or the user's terminal is left broken. The plan must handle TUI teardown robustly.
3. **Log file growth**: `~/.gpu-queue/logs/` accumulates. Acceptable for a local tool, but the plan should note it (maybe a note in README, or a future `gq clean-logs`). Not a blocker.
4. **TUI reading state concurrently with daemon writing it**: already handled by the existing `fcntl` locks on state.json/queue.json — TUI reads via `read_state`/`read_queue` which take the lock. No new race.

## Testing strategy

TUI is curses-rendered, harder to unit-test than the CLI. Approach:

1. **Pure logic, testable in pytest (no curses):** extract the TUI's state-to-rows logic into a function `_build_rows(state, queue, busy_cards) -> list[Row]` that returns the operations list given state. Test: with N running jobs + M queued, the rows include N `Stop:` lines + M `Cancel:` lines + the static ops, in the right order. With empty state, only static ops. This is the TUI's "what to show" brain, fully testable.
2. **F5/bash capture logic, testable:** extract `_run_bash_for_command() -> (cmd, cwd, env) | None` — the subprocess+bind+temp-file handoff. Test with a mocked bash that writes a known command to the temp file, assert the function returns it. (Real bash invocation tested manually.)
3. **Log redirection, testable:** the daemon's `_launch_job` change to redirect stdout/stderr to `~/.gpu-queue/logs/<id>.log` — test that a launched job's output lands in the log file (mock Popen, or a real echo job).
4. **curses rendering itself:** not unit-tested (visual). Manual verification: launch TUI, confirm bars/selection/refresh/no-flicker. The plan should list explicit manual checks.
5. **Integration:** `gq` no-arg enters TUI; `gq watch` still works; CLI commands still work; TUI add → job appears → daemon runs it → log file written → TUI shows it running → stop from TUI → job killed. One end-to-end manual script in the plan.

Existing 71 pytest + 8 bash completion tests must remain green (no regressions to CLI/daemon).

## Out of scope

- Truecolor / mouse support.
- zsh/fish support for the F5 bash-spawn (bash only; fallback input box is shell-agnostic).
- TUI as the daemon (rejected: TUI is a view).
- Auto-starting the daemon from `gq` no-arg (rejected: `gq watch` retains that).
- Log rotation / cleanup automation (noted, not built).
- Replacing CLI commands (they all remain).
- TUI sub-panels for task stdout tailing beyond a simple log-tail view.

## Migration note

After merge: re-sync `~/.local/bin/gq` (per memory `gq-dev-install-sync`); restart any running daemon (log redirection is a daemon-side change). Old `queue.json` jobs work as-is. Existing `gq watch` users see less output on the watch terminal (summary only) — task output moves to log files; document this in README.
