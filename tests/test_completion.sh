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
