#!/bin/zsh
set -eu

broeden_scripts="${0:A:h}"
broeden_profile="${ZDOTDIR:-$HOME}/.zshrc"
broeden_source_line="source ${(q)broeden_scripts}/terminal-shortcuts.zsh"
broeden_existing=""
if [[ -f "$broeden_profile" ]]; then
  broeden_existing="$(<"$broeden_profile")"
fi
if [[ $'\n'"$broeden_existing"$'\n' != *$'\n'"$broeden_source_line"$'\n'* ]]; then
  printf '\n# BroEdenBot restart shortcut\n%s\n' "$broeden_source_line" >> "$broeden_profile"
fi
print -r -- "bebotrestart is installed. Open a new terminal, or run: source ${(q)broeden_profile}"
