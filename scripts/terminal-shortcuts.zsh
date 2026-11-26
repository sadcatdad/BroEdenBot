# Source this file from ~/.zshrc to install the local BroEdenBot command.
# Reuse bebotdeploy's SSH settings rather than storing another server address.
typeset -g BROEDEN_TERMINAL_SCRIPTS="${${(%):-%x}:A:h}"

function bebotrestart() {
  command python3 "$BROEDEN_TERMINAL_SCRIPTS/bebotrestart.py" "$@"
}
