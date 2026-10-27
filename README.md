# IP-Modifier-Arch

Changes your IP address and gives information for connecting to devices over ethernet.

**`ipadd`**: a TUI for juggling laptop IP addresses when hopping between field devices.
Add / change / remove addresses, subnet masks and gateways, flip a port back
to DHCP, bypass a port's gateway so internet stays on wifi, and watch link
state, live traffic and the devices seen on the wire. Themed to match the
Control Freaks fetch (same palettes, and it opens on whichever one the fetch
last showed).

## Quick install

```sh
git clone https://github.com/Control-Freaks-Loops/IP-Modifier-Arch.git ipadd
cd ipadd
./install.sh
```

Then open a new terminal and type `ipadd`. The installer pulls
`python-textual` (plus `iproute2`, `iputils` and `hwdata` if missing) with
pacman and adds an `ipadd` fish function pointing at this folder. If you
move the folder, re-run it. To update later: `git pull` in the folder.

Built for **Arch / CachyOS + fish + NetworkManager** (niri / kitty, Nerd Font).

## Use

```
ipadd               open (picks the first wired port)
ipadd enp0s31f6     open on a specific port
ipadd --dry-run     look around and see what commands *would* run
ipadd --theme netrunner
```

| key | does |
|---|---|
| `1`–`9` / ↑↓ | pick a port |
| `tab` | move between ports / address table / neighbours |
| `a` | add an IP (IP, subnet mask and optional gateway; shows the subnet maths as you type) |
| `e` / `enter` | change the selected address |
| `d` | remove the selected address |
| `g` | set / clear the gateway |
| `b` | bypass the gateway on/off (internet stays on wifi) |
| `h` | back to DHCP (clears the saved static setup) |
| `x` | ping (gateway or the selected neighbour), and shows which port it really leaves on |
| `p` | next colour palette |
| `q` | quit |

The mask box takes `255.255.255.0`, `24` or `/24`; you can also type
`192.168.1.50/24` straight into the IP box.

Changes ask for your sudo password once (the TUI steps aside for the prompt).
Looking around needs no password.

## How changes are saved

* **Wired ports** use a NetworkManager profile called `ipadd-<port>`
  (e.g. `ipadd-enp1s0`). It's made on first change, and it outranks the
  automatic "Wired connection 1", so your setup survives pulling the cable,
  and NetworkManager doesn't wipe it when the port reconnects (which is what
  happened to addresses added with plain `ip`). Set it with no cable plugged
  in and it applies the moment you plug in. Your other profiles aren't touched.
* **Temporary** (tick box in the add form) uses plain `ip addr add`, the
  same way `ipmod` did. It's gone when the port reconnects.
* **Wifi** edits the active wifi profile. Ports NetworkManager doesn't manage
  (VPNs, docker bridges etc.) only get temporary changes.

Address colours: **static** = saved and applied, **pending** = saved but the
cable's out, **dhcp** = from a DHCP server, **live** = temporary.

## Routing warnings

If a subnet on a port is actually being sent somewhere else, the port card
shows a `ROUTING` warning, and the add form warns before you apply. The usual
culprits:

* the same subnet on two ports (wifi and cable both on 192.168.1.x)
* **Tailscale with `--accept-routes`**: if any tailnet machine advertises
  that subnet, Tailscale's routes win over the cable. Fix with
  `tailscale set --accept-routes=false`, or turn Tailscale off on site.

## Uninstall

```sh
rm ~/.config/fish/functions/ipadd.fish
nmcli connection delete ipadd-enp1s0     # one per port you used
```
