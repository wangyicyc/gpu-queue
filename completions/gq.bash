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
        COMPREPLY=($(compgen -W "watch add list cancel clear stop" -- "$cur"))
        return 0
    fi

    local subcmd="${COMP_WORDS[1]}"

    # gq add <prefix><Tab> → complete files/dirs.
    if [ "$subcmd" = "add" ] && [ "$COMP_CWORD" -ge 2 ]; then
        COMPREPLY=($(compgen -f -d -- "$cur"))
        return 0
    fi

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
}

complete -F _gq gq
