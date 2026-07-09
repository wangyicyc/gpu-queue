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
