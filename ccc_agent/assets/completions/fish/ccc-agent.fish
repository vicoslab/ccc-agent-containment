# fish completion for ccc-agent. Installed by pip into vendor_completions.d.
function __fish_ccc_agent_complete
    set -l tokens (commandline -opc)
    set -l current (commandline -ct)
    set -l cword (count $tokens)
    set tokens $tokens $current
    ccc-agent __complete fish $cword $tokens 2>/dev/null
end
complete -c ccc-agent -f -a "(__fish_ccc_agent_complete)"
