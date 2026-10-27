#!/usr/bin/env bash
# ipadd installer: Arch / CachyOS with fish + NetworkManager.
# Installs the few packages it needs and adds the `ipadd` fish command.
# Safe to re-run (e.g. after moving this folder).
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
say() { printf '\e[1;38;2;255;42;109m▞▚\e[0m %s\n' "$*"; }

# python-textual: the TUI · iproute2: ip · iputils: ping · hwdata: MAC vendor names
need=()
for pkg in python python-textual iproute2 iputils hwdata; do
    pacman -Q "$pkg" &>/dev/null || need+=("$pkg")
done
if ((${#need[@]})); then
    say "installing ${need[*]}"
    sudo pacman -S --needed "${need[@]}"
fi

if ! systemctl is-active --quiet NetworkManager; then
    say "NetworkManager isn't running: ipadd will still work, but changes are temporary (plain ip)"
fi

chmod +x "$DIR/ipadd.py"

fn_dir="${XDG_CONFIG_HOME:-$HOME/.config}/fish/functions"
mkdir -p "$fn_dir"
cat > "$fn_dir/ipadd.fish" <<EOF
function ipadd --description 'Control Freaks IP / port manager (TUI)'
    /usr/bin/python3 '$DIR/ipadd.py' \$argv
end
EOF
say "added fish command: ipadd  →  $DIR/ipadd.py"
say "open a new terminal (or run: source $fn_dir/ipadd.fish) and type ipadd"
