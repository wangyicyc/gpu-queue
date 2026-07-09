# Design: Bash Tab Completion for `gq`

**Date:** 2026-07-09
**Status:** Approved (pending spec review)

## Problem

`gq add` and `gq cancel` have no tab completion in bash. The user wants:
- **A.** `gq add <file-prefix><Tab>` completes filenames/paths (e.g. `gq add train<Tab>` → `train.py`).
- **C.** `gq cancel <prefix><Tab>` completes pending job IDs from the queue.

Neither works because bash has no knowledge of `gq`'s subcommand structure, and `gq` ships no completion script.

## Environment facts (verified 2026-07-09)

- System bash-completion is installed at `/usr/share/bash-completion/bash_completion`.
- `~/.bashrc` sources it (lines 112-114).
- bash-completion **auto-loads** any file placed in `~/.local/share/bash-completion/completions/<cmd>` named after the command — no `.bashrc` edit needed.
- The user-level dir `~/.local/share/bash-completion/completions` does not yet exist (will be created at install).
- `python3` is available (gq itself is Python) — use it to parse `queue.json`; do not depend on `jq`.
- Job IDs in `~/.gpu-queue/queue.json` are 4-char hex; `gq cancel` already supports prefix matching.

## Design

A single bash completion function `_gq`, registered with `complete -F _gq gq`, shipped as `completions/gq.bash` in the repo and installed to `~/.local/share/bash-completion/completions/gq` (auto-loaded).

### Behavior

`_gq()` inspects `$COMP_CWORD` (current word index) and `$COMP_WORDS` (the full command line):

1. **`COMP_CWORD == 1`** (the subcommand position) → complete from the fixed list `watch add list cancel clear`.
2. **Subcommand is `add`** → delegate to default file/path completion via `compgen -f -- "$cur"` (and directory completion with `-d`). This makes `gq add train<Tab>` → `train.py`, `gq add /home/<Tab>` → paths. Completion inside quotes is explicitly **not** handled (user's choice — simplest reliable behavior).
3. **Subcommand is `cancel`** → read pending job IDs from `~/.gpu-queue/queue.json` and complete with `compgen -W "<ids>" -- "$cur"` (prefix matching is built into compgen -W). Parse with:
   ```bash
   python3 -c "import json,os;print('\n'.join(j['id'] for j in json.load(open(os.path.expanduser('~/.gpu-queue/queue.json')))))" 2>/dev/null
   ```
   Missing/corrupt file → `2>/dev/null` swallows the error, returns empty (no completion offered).
4. **`watch`'s `--poll` value** → no completion (it's a number).
5. Any other case → no completion.

### Subcommand completion (zero-cost add-on)

`gq <Tab>` completing `watch add list cancel clear` is included — the function already needs the subcommand list for case 1, so exposing it at `COMP_CWORD==1` is free and natural.

### File layout

- **Create `completions/gq.bash`** — the completion script source (committed to the repo, travels with clones).
- **Install target** — `~/.local/share/bash-completion/completions/gq` (auto-loaded; not committed, user-local).
- **README** — add a "Shell completion" section in both 中文 and English explaining the one-line install (`cp completions/gq.bash ~/.local/share/bash-completion/completions/gq`).

### Install / loading

No `.bashrc` edit. bash-completion's user-dir auto-loader picks up the file by name. After install, a new shell (or `source ~/.local/share/bash-completion/completions/gq`) activates it. The script is idempotent and safe to source multiple times (re-registering `complete -F` just overwrites).

## Testing strategy

bash completion is shell logic, not testable by pytest. Use a self-contained bash test script `tests/test_completion.sh` that:

- Sources `completions/gq.bash`.
- Sets `COMP_WORDS` / `COMP_CWORD` / `cur` for each scenario, calls `_gq`, inspects `COMPREPLY`.
- Scenarios:
  1. `gq <Tab>` (`COMP_CWORD=1`) → COMPREPLY contains `watch add list cancel clear`.
  2. `gq add train<Tab>` (`COMP_CWORD=2`, cur=`train`) → COMPREPLY contains `train.py` when a `train.py` exists in cwd (create a temp file). Asserts filename completion works.
  3. `gq cancel <Tab>` (`COMP_CWORD=2`, cur empty) with a fake `~/.gpu-queue/queue.json` containing two jobs → COMPREPLY contains both IDs.
  4. `gq cancel 3f<Tab>` (cur=`3f`) with same queue → COMPREPLY contains only the matching `3f1a` (prefix filtering).
  5. `gq cancel <Tab>` with no `queue.json` → COMPREPLY empty, no error printed.

Run with `bash tests/test_completion.sh`; exit non-zero on any failure.

## Out of scope

- Completion inside single/double quotes (`gq add 'python <Tab>'`).
- `zsh` / `fish` completion (bash only — matches the user's shell: `/bin/bash`).
- `--poll` value completion.
- Auto-install of the completion on `gq` first run (explicit copy only; YAGNI).
