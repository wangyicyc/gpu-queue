# Bash Tab Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bash tab completion for `gq`: `gq <Tab>` completes subcommands, `gq add <prefix><Tab>` completes filenames/paths, `gq cancel <prefix><Tab>` completes pending job IDs from the queue.

**Architecture:** A single bash function `_gq`, registered via `complete -F _gq gq`, shipped as `completions/gq.bash` in the repo. It inspects `COMP_CWORD`/`COMP_WORDS`/`cur` and fills `COMPREPLY`: subcommand list at position 1, `compgen -f` for `add`, `compgen -W` of queue IDs for `cancel`. Installed by copying to `~/.local/share/bash-completion/completions/gq` (bash-completion auto-loads it — no `.bashrc` edit).

**Tech Stack:** Bash 4+ (completion functions); `python3` to parse `queue.json` (always present — gq is Python; avoid `jq` dependency). Tests are a bash script `tests/test_completion.sh` (not pytest — completion is shell logic).

## Global Constraints

- **Zero external runtime deps beyond what gq already needs** — `python3` (already required by gq); do NOT require `jq` or `bash-completion` to be installed for the *script itself* to function (only for auto-loading, which is the user's shell setup).
- **Bash only** — no zsh/fish (spec out of scope; user's shell is `/bin/bash`).
- **No quote-internal completion** — `gq add 'python <Tab>'` is explicitly out of scope; only bare `gq add <prefix><Tab>` is supported.
- **No `.bashrc` edits** — install is a file copy to the auto-loading completions dir.
- **The completion script must not depend on `gq` being on PATH or importable** — it parses `~/.gpu-queue/queue.json` directly with python3, not by shelling out to `gq list` (faster, no subprocess-per-tab, and works even if gq isn't installed yet).
- **Tests are bash, not pytest** — run with `bash tests/test_completion.sh`. The pytest suite (50 tests) must remain green and untouched.
- **Current branch:** `main` (this is a small feature; will branch to `feat/completion` at execution time).

---

## File Structure

- **Create:** `completions/gq.bash` — the completion script (committed; travels with clones). Contains `_gq()` + `complete -F _gq gq`.
- **Create:** `tests/test_completion.sh` — bash test harness; sources `completions/gq.bash`, drives `_gq` with synthetic `COMP_WORDS`, asserts on `COMPREPLY`.
- **Modify:** `README.md` — add a "Shell completion" section (中文 + English) with the one-line install.

No changes to the `gq` script itself.

---

## Task 1: Create the completion script with subcommand + `add` completion

**Files:**
- Create: `completions/gq.bash`
- Test: `tests/test_completion.sh`

**Interfaces:**
- Produces: a bash function `_gq()` (signature: `_gq()` — reads globals `COMP_WORDS`, `COMP_CWORD`, `cur`; writes global `COMPREPLY`) and a `complete -F _gq gq` registration. Task 2 will extend `_gq` for `cancel`; this task builds the skeleton + subcommand + `add` paths.

- [ ] **Step 1: Write the completion script skeleton (subcommand + add + file completion)**

Create `completions/gq.bash` with this exact content:

```bash
# bash completion for gq — GPU Task Queue
# Install: cp completions/gq.bash ~/.local/share/bash-completion/completions/gq
# (bash-completion auto-loads it by filename; no .bashrc edit needed.)
# Source manually to try without installing: . completions/gq.bash

_gq() {
    local cur prev words cword
    _init_completion 2>/dev/null || {
        cur="${COMP_WORDS[COMP_CWORD]}"
        prev="${COMP_WORDS[COMP_CWORD-1]}"
    }

    # Subcommand position: complete the fixed subcommand list.
    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=($(compgen -W "watch add list cancel clear" -- "$cur"))
        return 0
    fi

    local subcmd="${COMP_WORDS[1]}"

    # gq add <prefix><Tab> → complete files/dirs.
    if [ "$subcmd" = "add" ] && [ "$COMP_CWORD" -ge 2 ]; then
        COMPREPLY=($(compgen -f -d -- "$cur"))
        return 0
    fi

    # gq cancel <prefix><Tab> → completed in Task 2.
    # gq watch --poll <n> → no completion (number).
    # Default: no completion.
    COMPREPLY=()
    return 0
}

complete -F _gq gq
```

Notes for the implementer:
- `_init_completion` is a bash-completion helper that sets `cur`/`prev`/`words`/`cword`; the `|| { ... }` fallback sets `cur`/`prev` when bash-completion isn't loaded (keeps the function usable in a bare `bash` test shell that sources the file without the full bash-completion framework). The tests below run in a plain `bash` and rely on the fallback path — **do not remove the fallback**.
- `compgen -f -d -- "$cur"` lists files AND directories matching `cur`.

- [ ] **Step 2: Write the test harness scaffolding + first two scenarios**

Create `tests/test_completion.sh` with this exact content:

```bash
#!/usr/bin/env bash
# Test harness for completions/gq.bash. Runs in plain bash (no bash-completion
# framework) — relies on _gq's _init_completion fallback. Exits non-zero on
# the first failing assertion.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
# shellcheck disable=SC1091
. "$REPO/completions/gq.bash"

PASS=0
FAIL=0

assert_contains() {
    # assert_contains <label> <needle> <"haystack line(s)">
    local label="$1" needle="$2" haystack="$3"
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        PASS=$((PASS+1)); printf 'ok   %s\n' "$label"
    else
        FAIL=$((FAIL+1)); printf 'FAIL %s (needle=%q not in reply)\n' "$label" "$needle"
        printf '     reply was:\n'
        printf '%s\n' "$haystack" | sed 's/^/       | /'
    fi
}

assert_equals_sorted() {
    # assert_equals_sorted <label> <expected newline-list> <actual newline-list>
    local label="$1" expected="$2" actual="$3"
    local e a
    e=$(printf '%s\n' "$expected" | sort)
    a=$(printf '%s\n' "$actual" | sort)
    if [ "$e" = "$a" ]; then
        PASS=$((PASS+1)); printf 'ok   %s\n' "$label"
    else
        FAIL=$((FAIL+1)); printf 'FAIL %s\n' "$label"
        printf '     expected (sorted): %s\n' "$e"
        printf '     actual   (sorted): %s\n' "$a"
    fi
}

drive() {
    # drive <COMP_CWORD> <COMP_WORDS...>  — last word is $cur.
    # Sets the globals _gq reads, calls _gq, prints COMPREPLY.
    COMP_CWORD="$1"; shift
    COMP_WORDS=("$@")
    cur="${COMP_WORDS[$COMP_CWORD]}"
    COMPREPLY=()
    _gq
    # Join COMPREPLY array with newlines for assertion helpers.
    if [ "${#COMPREPLY[@]}" -eq 0 ]; then printf ''
    else printf '%s\n' "${COMPREPLY[@]}"; fi
}

# ---- Scenario 1: gq <Tab> completes subcommands ----
out="$(drive 1 gq "")"
assert_equals_sorted "gq <Tab> lists subcommands" \
    "watch
add
list
cancel
clear" \
    "$out"

# ---- Scenario 2: gq add train<Tab> completes matching files ----
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
( cd "$TMPD" && touch train.py train_other.py model.pth )
out="$(cd "$TMPD" && drive 2 gq add train)"
# cur=train; compgen -f -d -- train in $TMPD lists both .py files (not .pth? it
# lists .pth too — all files). Assert the .py ones are present.
assert_contains "gq add train<Tab> completes train.py" "train.py" "$out"
assert_contains "gq add train<Tab> completes train_other.py" "train_other.py" "$out"

# cur empty: all files appear
out="$(cd "$TMPD" && drive 2 gq add "")"
assert_contains "gq add <Tab> lists cwd files" "model.pth" "$out"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
```

- [ ] **Step 3: Run the tests — expect PASS for scenarios 1-2**

Run: `bash tests/test_completion.sh`
Expected: output ending with `4 passed, 0 failed` (4 assertions: subcommands sorted, train.py contains, train_other.py contains, model.pth contains) and exit code 0.

If `_init_completion` is not available in the test shell, the fallback sets `cur` — the harness runs in plain `bash`, so the fallback is what's exercised. If a scenario fails, debug the fallback `cur` assignment.

- [ ] **Step 4: Commit**

```bash
git add completions/gq.bash tests/test_completion.sh
git commit -m "feat(gq): bash completion for subcommands and add"
```

---

## Task 2: Add `cancel` ID completion (reads queue.json)

**Files:**
- Modify: `completions/gq.bash` — add the `cancel` branch to `_gq`
- Test: `tests/test_completion.sh` — add scenarios 3, 4, 5 (cancel prefix match, cancel empty, cancel no-queue)

**Interfaces:**
- Consumes: `_gq()` from Task 1 (extends it; does not change the existing subcommand/add behavior).
- Produces: `cancel` branch populates `COMPREPLY` from `~/.gpu-queue/queue.json` IDs via `python3`. Uses `$HOME` to locate the file (expanduser-equivalent: `${HOME}/.gpu-queue/queue.json`).

- [ ] **Step 1: Add the `cancel` branch to `_gq`**

In `completions/gq.bash`, replace the placeholder block:

```bash
    # gq cancel <prefix><Tab> → completed in Task 2.
    # gq watch --poll <n> → no completion (number).
    # Default: no completion.
    COMPREPLY=()
    return 0
```

with:

```bash
    # gq cancel <prefix><Tab> → complete pending job IDs from queue.json.
    if [ "$subcmd" = "cancel" ] && [ "$COMP_CWORD" -ge 2 ]; then
        local ids
        ids="$(python3 -c "
import json, os
p = os.path.join(os.environ.get('HOME', ''), '.gpu-queue', 'queue.json')
try:
    with open(p) as f:
        q = json.load(f)
    print('\n'.join(j['id'] for j in q if isinstance(j, dict) and 'id' in j))
except (OSError, ValueError, KeyError):
    pass
" 2>/dev/null)"
        COMPREPLY=($(compgen -W "$ids" -- "$cur"))
        return 0
    fi

    # gq watch --poll <n> → no completion (number).
    # Default: no completion.
    COMPREPLY=()
    return 0
```

Notes:
- Uses `os.environ.get('HOME', '')` instead of `expanduser` to be robust if HOME is unset; falls back to empty path → file-not-found → empty `ids`.
- `try/except` swallows missing file, corrupt JSON, missing `id` key — all yield empty `ids`, hence empty completion (no error printed, thanks to `2>/dev/null`).

- [ ] **Step 2: Add cancel test scenarios to the harness**

In `tests/test_completion.sh`, **before** the final `printf '\n%d passed, %d failed\n' ...` summary line, add:

```bash

# ---- Scenario 3: gq cancel <Tab> lists all pending IDs ----
# Use an isolated fake ~/.gpu-queue under a temp HOME so we never touch the
# real one, and the test is hermetic.
FAKEHOME="$(mktemp -d)"
mkdir -p "$FAKEHOME/.gpu-queue"
printf '%s' '[{"id":"3f1a","cmd":"a"},{"id":"a9c2","cmd":"b"}]' \
    > "$FAKEHOME/.gpu-queue/queue.json"

out="$(HOME="$FAKEHOME" drive 2 gq cancel "")"
assert_equals_sorted "gq cancel <Tab> lists all IDs" \
    "3f1a
a9c2" \
    "$out"

# ---- Scenario 4: gq cancel 3f<Tab> filters to matching prefix ----
out="$(HOME="$FAKEHOME" drive 2 gq cancel 3f)"
assert_equals_sorted "gq cancel 3f<Tab> filters prefix" "3f1a" "$out"

# ---- Scenario 5: gq cancel <Tab> with no queue.json → empty, no error ----
EMPTYHOME="$(mktemp -d)"
out="$(HOME="$EMPTYHOME" drive 2 gq cancel "")"
assert_equals_sorted "gq cancel <Tab> no queue → empty" "" "$out"

# Cleanup temp HOMEs (the EXIT trap still removes TMPD from Scenario 2).
rm -rf "$FAKEHOME" "$EMPTYHOME"
```

- [ ] **Step 3: Run the full harness — expect all scenarios pass**

Run: `bash tests/test_completion.sh`
Expected: output ending with `7 passed, 0 failed` (4 from Task 1 + 3 new: cancel-list, cancel-prefix, cancel-empty) and exit code 0.

If scenario 5 fails with a non-empty reply, the `try/except` or `2>/dev/null` isn't swallowing the missing-file error — check the python3 invocation runs under `HOME="$EMPTYHOME"`.

- [ ] **Step 4: Verify the pytest suite is untouched and still green**

Run: `PYTHONPATH= python -m pytest tests/ -q`
Expected: `50 passed` (the completion tests are bash, not collected by pytest; pytest still sees only the 50 gq tests). The `PYTHONPATH=` prefix is required on this machine — see memory note `ros-pytest-workaround`.

- [ ] **Step 5: Commit**

```bash
git add completions/gq.bash tests/test_completion.sh
git commit -m "feat(gq): cancel completes pending job IDs from queue.json"
```

---

## Task 3: Document shell completion in README

**Files:**
- Modify: `README.md` — add a "Shell completion" section in 中文 (after `### 测试` or near commands) and English (after `### Tests`).

- [ ] **Step 1: Add the 中文 section**

In `README.md`, in the 中文 half, the `### 测试` section currently begins with:

```
### 测试

```bash
git clone https://github.com/wangyicyc/gpu-queue.git
```

Insert a new section **immediately before** `### 测试`:

```
### Shell 补全（可选）

`gq` 带 bash 补全：`gq <Tab>` 补全子命令，`gq add train<Tab>` 补全文件名，`gq cancel <Tab>` 补全队列里的任务 ID。

```bash
# 装到 bash-completion 自动加载目录（无需改 .bashrc）
mkdir -p ~/.local/share/bash-completion/completions
cp completions/gq.bash ~/.local/share/bash-completion/completions/gq

# 开个新终端即生效；或当场 source：
. ~/.local/share/bash-completion/completions/gq
```

> 需要 bash-completion（大多数发行版默认装了）。补全脚本在仓库的 `completions/gq.bash`，clone 后即可用。

```

- [ ] **Step 2: Add the English section**

In `README.md`, in the English half, the `### Tests` section currently begins with:

```
### Tests

```bash
python -m pytest tests/ -v    # 38 tests
```

Insert a new section **immediately before** `### Tests`:

```
### Shell completion (optional)

`gq` ships a bash completion: `gq <Tab>` completes subcommands, `gq add train<Tab>` completes filenames, `gq cancel <Tab>` completes pending job IDs from the queue.

```bash
# Install to bash-completion's auto-load dir (no .bashrc edit needed)
mkdir -p ~/.local/share/bash-completion/completions
cp completions/gq.bash ~/.local/share/bash-completion/completions/gq

# A new shell picks it up; or source it now:
. ~/.local/share/bash-completion/completions/gq
```

> Requires bash-completion (installed by default on most distros). The completion script lives at `completions/gq.bash` in the repo — usable after `git clone`.

```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document bash shell completion install"
```

---

## Self-Review

**1. Spec coverage:**
- "gq add <prefix><Tab> completes filenames/paths" → Task 1 Step 1 (`compgen -f -d`), tested Scenarios 1-2 (Task 1 Step 2/3). ✓
- "gq cancel <prefix><Tab> completes pending job IDs (prefix match)" → Task 2 Step 1, tested Scenarios 3-4. ✓
- "gq <Tab> completes subcommands" (zero-cost add-on) → Task 1 Step 1, tested Scenario 1. ✓
- "Parse queue.json with python3, not jq" → Task 2 Step 1 uses python3. ✓
- "Missing/corrupt file → empty, no error" → Task 2 Step 1 try/except + 2>/dev/null, tested Scenario 5. ✓
- "Shipped as completions/gq.bash, installed to auto-load dir, no .bashrc edit" → Task 1 creates the file; Task 3 documents the install. ✓
- "Tests: bash script, 5 scenarios" → Tasks 1-2 build `tests/test_completion.sh` with 5 scenarios (subcommand, add-file, cancel-list, cancel-prefix, cancel-empty). ✓
- "No quote-internal completion / no zsh / no --poll / no auto-install" → explicitly not implemented. ✓

**2. Placeholder scan:** No TBD/TODO. Every step has full code or exact text. The `compgen -f -d` behavior was verified live before writing (files + dirs match `cur`).

**3. Type/consistency consistency:** `_gq()` signature and the `cur`/`COMP_WORDS`/`COMP_CWORD`/`COMPREPLY` globals are used consistently in both tasks and the test harness. The `drive()` helper's argument order (`COMP_CWORD` then `COMP_WORDS...`) matches every scenario call. The `compgen -f -d -- "$cur"` in Task 1 matches the verified behavior. `subcmd="${COMP_WORDS[1]}"` is set in Task 1 and read in Task 2 — consistent.

One subtlety the implementer must honor (already noted in Task 1 Step 1): the `_init_completion 2>/dev/null || { ... fallback ... }` is load-bearing for the test harness, which runs in plain bash without the bash-completion framework. Do not simplify it away.

No gaps. Plan is complete.
