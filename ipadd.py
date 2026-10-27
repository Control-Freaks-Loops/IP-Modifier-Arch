#!/usr/bin/env python3
"""ipadd: Control Freaks network port manager (TUI).

Add, change and remove IPv4 addresses / subnet masks / gateways on a port,
put it back on DHCP, bypass its gateway so internet stays on wifi, and watch
link state, live traffic and the devices seen on the wire.

Ethernet changes are saved in a NetworkManager profile called "ipadd-<port>",
so they survive pulling the cable (NetworkManager wipes addresses added with
plain `ip` the next time the port connects). Tick "temporary" in the add form
to use plain `ip` instead, like the old ipmod script did.

  ipadd                 launch
  ipadd PORT            launch with PORT selected (e.g. ipadd enp1s0)
  ipadd --dry-run       show the commands instead of running them
  ipadd --theme NAME    start on a given Control Freaks palette
"""
import collections
import getpass
import importlib.util
import ipaddress
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field

try:
    from rich.text import Text
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.theme import Theme
    from textual.widget import Widget
    from textual.widgets import (Button, Checkbox, DataTable, Footer, Input, Label, Log,
                                 OptionList, Sparkline, Static)
    from textual.widgets.option_list import Option
except ImportError:
    sys.exit("ipadd needs Textual:  sudo pacman -S python-textual   (or run install.sh)")

CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "controlfreaks")
CF_FETCH = os.path.expanduser("~/.config/fastfetch/controlfreaks/cf-fetch.py")
OUI_DB = "/usr/share/hwdata/oui.txt"
PROFILE_PREFIX = "ipadd-"
PROFILE_PRIORITY = "50"     # beats NetworkManager's auto "Wired connection N" profiles
HIST = 120                  # seconds of traffic history in the sparklines
SCAN_EVERY = 3              # seconds between full rescans (addresses, routes, neighbours)
DRY_RUN = False
IS_ROOT = os.geteuid() == 0

# ── Palettes ────────────────────────────────────────────────────────────────
# Same set as cf-fetch (read from it when installed, so the two stay in sync).
PALETTES = {
    "SAMURAI":     {"fill": ["#fcee0a", "#ffe600"],            "shadow": "#02d7f2", "glitch": "#ff2a6d"},
    "NIGHT CITY":  {"fill": ["#fcee0a", "#ff9f00", "#ff003c"], "shadow": "#00f0ff", "glitch": "#ff2a6d"},
    "NEON DRIFT":  {"fill": ["#ff2a6d", "#d300c5", "#05d9e8"], "shadow": "#fcee0a", "glitch": "#ffffff"},
    "NETRUNNER":   {"fill": ["#00f0ff", "#3d5afe", "#9d00ff"], "shadow": "#ff2a6d", "glitch": "#fcee0a"},
    "SYNTHWAVE":   {"fill": ["#f9c80e", "#f86624", "#ea3546", "#662e9b"], "shadow": "#00f0ff", "glitch": "#ff71ce"},
    "MILITECH":    {"fill": ["#ccff00", "#39ff14", "#00e5ff"], "shadow": "#ff00aa", "glitch": "#ffffff"},
    "VAPORWAVE":   {"fill": ["#ff71ce", "#b967ff", "#01cdfe"], "shadow": "#fffb96", "glitch": "#05ffa1"},
    "ARASAKA":     {"fill": ["#ffffff", "#ff4d6d", "#c1001f"], "shadow": "#00f0ff", "glitch": "#ff003c"},
    "AFTERLIFE":   {"fill": ["#ffd319", "#ff2975", "#8c1eff"], "shadow": "#00ffd0", "glitch": "#ffffff"},
}


def load_palettes():
    try:
        spec = importlib.util.spec_from_file_location("cf_fetch", CF_FETCH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if mod.PALETTES:
            return dict(mod.PALETTES)
    except Exception:
        pass
    return dict(PALETTES)


def fetch_palette(names):
    """The palette cf-fetch showed last, so the TUI matches the terminal."""
    try:
        return names[int(open(os.path.join(CACHE, "palette_index")).read().strip()) % len(names)]
    except (OSError, ValueError):
        return names[0]


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_hex(rgb):
    return "#%02x%02x%02x" % rgb


def _lin(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _gam(c):
    c = 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return max(0, min(255, round(c * 255)))


def to_oklab(rgb):
    r, g, b = (_lin(c) for c in rgb)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def from_oklab(lab):
    L, a, b = lab
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (_gam(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
            _gam(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
            _gam(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s))


class Gradient:
    def __init__(self, stops):
        self.labs = [to_oklab(hex_rgb(s)) for s in stops]
        if len(self.labs) == 1:
            self.labs *= 2

    def at(self, t):
        t = max(0.0, min(1.0, t)) * (len(self.labs) - 1)
        i = min(int(t), len(self.labs) - 2)
        f = t - i
        a, b = self.labs[i], self.labs[i + 1]
        return rgb_hex(from_oklab(tuple(a[k] + (b[k] - a[k]) * f for k in range(3))))


def tint(h, amount):
    """Blend toward white by `amount` (0..1)."""
    return rgb_hex(tuple(round(c + (255 - c) * amount) for c in hex_rgb(h)))


def grad_text(s, grad, bold=False, t0=0.0, t1=1.0):
    out = Text()
    n = max(1, len(s) - 1)
    for i, ch in enumerate(s):
        out.append(ch, style=("bold " if bold else "") + grad.at(t0 + (t1 - t0) * i / n))
    return out


# ── Icons (Nerd Font) ───────────────────────────────────────────────────────
ICON = {
    "ethernet": "\U000f0200", "wifi": "\U000f05a9", "tun": "\U000f0582", "loopback": "\U000f006a",
    "bridge": "\U000f0389", "virtual": "\U000f0389",
    "speed": "\U000f04c5", "mac": "\U000f061a", "profile": "\U000f0493", "mode": "\U000f0a5f",
    "gateway": "\U000f0318", "internet": "\U000f059f", "link": "\U000f0337",
}

# ── Running commands ────────────────────────────────────────────────────────
class CmdError(Exception):
    pass


def sh(cmd, timeout=5):
    """Read-only command; stdout or '' on any failure."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def sh_json(cmd):
    try:
        return json.loads(sh(cmd) or "[]")
    except ValueError:
        return []


class Runner:
    """Runs changes as root (sudo -n; the app unlocks sudo first) and keeps a log."""

    def __init__(self):
        self.cmds = []

    def __call__(self, cmd, timeout=30, ok=(0,)):
        self.cmds.append(shlex.join(cmd))
        if DRY_RUN:
            return ""
        full = cmd if IS_ROOT else ["sudo", "-n", *cmd]
        try:
            r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise CmdError(f"timed out: {shlex.join(cmd)}")
        if r.returncode not in ok:
            lines = (r.stderr or r.stdout).strip().splitlines()
            raise CmdError(lines[-1] if lines else f"{cmd[0]} failed (exit {r.returncode})")
        return r.stdout


# ── System state ────────────────────────────────────────────────────────────
@dataclass
class Profile:
    name: str
    method: str = "auto"
    addresses: list = field(default_factory=list)
    gateway: str = ""
    never_default: bool = False
    exists: bool = True
    active: bool = False


@dataclass
class Addr:
    cidr: str
    source: str     # static | pending | dhcp | live

    @property
    def iface(self):
        return ipaddress.IPv4Interface(self.cidr)


@dataclass
class Port:
    name: str
    kind: str
    admin_up: bool
    carrier: object         # True / False / None (port disabled)
    mac: str = ""
    mtu: int = 0
    speed: int = 0
    duplex: str = ""
    driver: str = ""
    nm_state: str = ""
    nm_conn: str = ""
    profile: object = None  # Profile, or None when NetworkManager doesn't manage it
    addrs: list = field(default_factory=list)
    v6: int = 0
    gateways: list = field(default_factory=list)   # (gateway, metric) default routes out this port
    neigh: list = field(default_factory=list)      # (ip, mac, state)
    hijack: list = field(default_factory=list)     # (subnet, port) where that subnet really goes


KIND_ORDER = {"ethernet": 0, "wifi": 1, "bridge": 2, "virtual": 3, "tun": 4, "loopback": 5}


def sysfs(name, attr):
    try:
        with open(f"/sys/class/net/{name}/{attr}") as f:
            return f.read().strip()
    except OSError:
        return None


def port_kind(name, link_type):
    base = f"/sys/class/net/{name}"
    if link_type == "loopback":
        return "loopback"
    if os.path.isdir(base + "/wireless") or os.path.exists(base + "/phy80211"):
        return "wifi"
    if os.path.exists(base + "/tun_flags"):
        return "tun"
    if os.path.isdir(base + "/bridge"):
        return "bridge"
    if os.path.exists(base + "/device"):
        return "ethernet"       # onboard and USB adapters alike
    return "virtual"


def split_terse(line):
    """Split one line of `nmcli -t` output (':' separated, '\\' escapes)."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur]


def nm_running():
    return sh(["nmcli", "-t", "-f", "RUNNING", "general"]).strip() == "running"


def nm_devices():
    devs = {}
    for line in sh(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]).splitlines():
        parts = split_terse(line)
        if len(parts) >= 4:
            devs[parts[0]] = (parts[1], parts[2], parts[3])
    return devs


def read_profile(name):
    out = sh(["nmcli", "-t", "-f", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.never-default",
              "connection", "show", "id", name])
    if not out:
        return Profile(name, exists=False)
    vals = {}
    for line in out.splitlines():
        key, _, val = line.partition(":")
        vals[key] = val.strip()
    gw = vals.get("ipv4.gateway", "")
    return Profile(name, method=vals.get("ipv4.method", "auto"),
                   addresses=[a.strip() for a in vals.get("ipv4.addresses", "").split(",") if a.strip()],
                   gateway="" if gw in ("", "--") else gw,
                   never_default=vals.get("ipv4.never-default") == "yes")


def scan():
    """Snapshot of every port: link, addresses, NM profile, routes, neighbours."""
    links = sh_json(["ip", "-j", "addr", "show"])
    routes = [r for r in sh_json(["ip", "-j", "-4", "route", "show", "default"])
              if "linkdown" not in r.get("flags", [])]
    neigh = sh_json(["ip", "-j", "-4", "neigh", "show"])
    nm = nm_devices() if nm_running() else {}
    ports = []
    for l in links:
        name = l.get("ifname")
        if not name:
            continue
        kind = port_kind(name, l.get("link_type"))
        flags = l.get("flags", [])
        carrier = sysfs(name, "carrier")
        speed = sysfs(name, "speed")
        drv = os.path.realpath(f"/sys/class/net/{name}/device/driver")
        p = Port(name, kind, admin_up="UP" in flags,
                 carrier=None if carrier is None else carrier == "1",
                 mac=l.get("address", "") if kind != "loopback" else "",
                 mtu=l.get("mtu", 0),
                 speed=int(speed) if speed and speed.lstrip("-").isdigit() and int(speed) > 0 else 0,
                 duplex=sysfs(name, "duplex") or "",
                 driver=os.path.basename(drv) if os.path.exists(drv) else "")
        if name in nm:
            _, p.nm_state, p.nm_conn = nm[name]
            if p.nm_state.startswith("unmanaged"):
                pass
            elif kind == "ethernet":
                p.profile = read_profile(PROFILE_PREFIX + name)
            elif kind == "wifi" and p.nm_conn:
                p.profile = read_profile(p.nm_conn)
            if p.profile:
                p.profile.active = p.nm_conn == p.profile.name
        saved = set(p.profile.addresses) if p.profile else set()
        seen = set()
        for a in l.get("addr_info", []):
            if a.get("family") == "inet6":
                p.v6 += 1
                continue
            if a.get("family") != "inet":
                continue
            cidr = f"{a['local']}/{a['prefixlen']}"
            seen.add(cidr)
            src = "static" if cidr in saved and p.profile.active else "dhcp" if a.get("dynamic") else "live"
            p.addrs.append(Addr(cidr, src))
        p.addrs += [Addr(c, "pending") for c in p.profile.addresses if c not in seen] if p.profile else []
        p.gateways = [(r.get("gateway", ""), r.get("metric", 0)) for r in routes if r.get("dev") == name]
        p.hijack = route_hijacks(p)
        p.neigh = [(n["dst"], n.get("lladdr", ""), "/".join(n.get("state", [])).lower())
                   for n in neigh if n.get("dev") == name and n.get("lladdr")
                   and not set(n.get("state", [])) & {"FAILED", "INCOMPLETE"}]
        ports.append(p)
    ports.sort(key=lambda p: (KIND_ORDER.get(p.kind, 9), p.name))
    best = min(routes, key=lambda r: r.get("metric", 0), default=None)
    internet = (best["dev"], best.get("gateway", "")) if best else None
    return ports, internet


def route_hijacks(p):
    """Subnets on this port whose traffic the kernel actually sends somewhere else
    (another port on the same subnet, or Tailscale's table 52 with accept-routes)."""
    out = []
    if p.kind == "loopback" or not p.carrier:
        return out
    for a in p.addrs:
        net = a.iface.network
        if a.source == "pending" or net.prefixlen > 30:
            continue
        gws = [ipaddress.IPv4Address(g) for g, _ in p.gateways if g]
        probe = next((g for g in gws if g in net), None) or next(h for h in (net.network_address + 1,
                                                                              net.network_address + 2)
                                                                 if h != a.iface.ip)
        r = sh_json(["ip", "-j", "route", "get", str(probe)])
        if r and r[0].get("dev") not in (p.name, "lo"):
            out.append((str(net), r[0]["dev"]))
    return out


def competing_routes(net, port):
    """Routes in any table that would pull traffic for `net` away from `port`."""
    out = []
    for r in sh_json(["ip", "-j", "-4", "route", "show", "table", "all"]):
        dst, dev = r.get("dst"), r.get("dev")
        if dst in (None, "default") or dev in (None, port, "lo") or r.get("type") in ("local", "broadcast"):
            continue
        try:
            n = ipaddress.IPv4Network(dst, strict=False)
        except ValueError:
            continue
        if n.overlaps(net):
            out.append((str(n), dev, str(r.get("table", "main"))))
    return out


def read_counters(name):
    def n(attr):
        v = sysfs(name, "statistics/" + attr)
        return int(v) if v and v.isdigit() else 0
    return {k: n(k) for k in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets",
                               "rx_errors", "tx_errors", "rx_dropped", "tx_dropped")}


_OUI = None


def load_oui():
    global _OUI
    db = {}
    try:
        with open(OUI_DB, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "(hex)" in line:
                    pre, _, vendor = line.partition("(hex)")
                    db[pre.strip().replace("-", "").upper()] = vendor.strip()
    except OSError:
        pass
    _OUI = db


def vendor(mac):
    if not mac:
        return ""
    if int(mac[:2], 16) & 2:
        return "(random / locally set)"
    return (_OUI or {}).get(mac.replace(":", "")[:6].upper(), "")


# ── Validation ──────────────────────────────────────────────────────────────
def parse_prefix(text):
    t = text.strip().lstrip("/")
    if not t:
        return None
    if t.isdigit():
        if 0 < int(t) <= 32:
            return int(t)
        raise ValueError("prefix length must be 1-32")
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{t}").prefixlen
    except ValueError:
        raise ValueError(f"{text.strip()} isn't a valid subnet mask")


def parse_address(ip_text, mask_text):
    ip_text = ip_text.strip()
    if not ip_text:
        raise ValueError("enter an IP address")
    ip, _, given = ip_text.partition("/")
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        raise ValueError(f"{ip} isn't a valid IPv4 address")
    pre_ip = parse_prefix(given) if given else None
    pre_mask = parse_prefix(mask_text)
    if pre_ip and pre_mask and pre_ip != pre_mask:
        raise ValueError(f"/{pre_ip} on the address doesn't match the mask (/{pre_mask})")
    prefix = pre_ip or pre_mask
    if not prefix:
        raise ValueError("enter a subnet mask (e.g. 255.255.255.0 or 24)")
    iface = ipaddress.IPv4Interface(f"{addr}/{prefix}")
    if addr.is_loopback or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        raise ValueError(f"{addr} can't be used on a port")
    if prefix <= 30 and addr in (iface.network.network_address, iface.network.broadcast_address):
        which = "network" if addr == iface.network.network_address else "broadcast"
        raise ValueError(f"{addr} is the {which} address of {iface.network}")
    return iface


def parse_gateway(text, networks, own=()):
    text = text.strip()
    if not text:
        return ""
    try:
        gw = ipaddress.IPv4Address(text)
    except ValueError:
        raise ValueError(f"gateway {text} isn't a valid IPv4 address")
    if gw in own:
        raise ValueError("gateway can't be the port's own address")
    if networks and not any(gw in n for n in networks):
        raise ValueError(f"gateway {gw} isn't inside {', '.join(map(str, networks))}")
    return str(gw)


def subnet_summary(iface):
    net = iface.network
    if net.prefixlen >= 31:
        hosts = f"{net.num_addresses} (point-to-point)"
    else:
        first, last = net.network_address + 1, net.broadcast_address - 1
        hosts = f"{first} – {last}  ({net.num_addresses - 2:,})"
    return net, iface.netmask, hosts, net.broadcast_address


# ── Changes ─────────────────────────────────────────────────────────────────
# Each takes a Runner and the Port snapshot, and returns a status message.
def _ensure_profile(run, p):
    prof = p.profile
    if not prof.exists:
        run(["nmcli", "connection", "add", "type", "ethernet", "ifname", p.name, "con-name", prof.name,
             "connection.autoconnect", "yes", "connection.autoconnect-priority", PROFILE_PRIORITY,
             "ipv4.method", "auto"])
        prof.exists = True
    return prof.name


def _activate(run, p, name):
    if not p.carrier:
        return "saved — applies when the cable is plugged in"
    run(["nmcli", "--wait", "20", "connection", "up", "id", name, "ifname", p.name], timeout=40)
    return "applied"


def _set_profile(run, p, addresses, gateway, never_default=None):
    name = _ensure_profile(run, p)
    cmd = ["nmcli", "connection", "modify", "id", name,
           "ipv4.method", "manual" if addresses else "auto",
           "ipv4.addresses", ",".join(addresses), "ipv4.gateway", gateway]
    if never_default is not None:
        cmd += ["ipv4.never-default", "yes" if never_default else "no"]
    run(cmd)
    return _activate(run, p, name)


def _live_gateway(run, p, gw):
    run(["ip", "route", "replace", "default", "via", gw, "dev", p.name, "metric", "100"])


def do_add(run, p, form):
    cidr, gw = form["cidr"], form["gw"]
    if form["temp"] or not p.profile:
        if not p.admin_up:
            run(["ip", "link", "set", "dev", p.name, "up"])
        run(["ip", "addr", "add", cidr, "dev", p.name])
        if gw:
            _live_gateway(run, p, gw)
        return f"{cidr} added (temporary — gone when the port reconnects)"
    addrs = [a for a in p.profile.addresses if a != cidr] + [cidr]
    return f"{cidr} " + _set_profile(run, p, addrs, gw)


def do_edit(run, p, old, form):
    cidr, gw = form["cidr"], form["gw"]
    if old.source in ("static", "pending") and p.profile:
        addrs = list(dict.fromkeys(cidr if a == old.cidr else a for a in p.profile.addresses))
        return f"{old.cidr} → {cidr} " + _set_profile(run, p, addrs, gw)
    run(["ip", "addr", "del", old.cidr, "dev", p.name])
    run(["ip", "addr", "add", cidr, "dev", p.name])
    if gw:
        _live_gateway(run, p, gw)
    return f"{old.cidr} → {cidr} (temporary)"


def do_remove(run, p, a):
    if a.source in ("static", "pending") and p.profile:
        addrs = [x for x in p.profile.addresses if x != a.cidr]
        gw = p.profile.gateway
        if gw and not any(ipaddress.IPv4Address(gw) in ipaddress.IPv4Interface(x).network for x in addrs):
            gw = ""
        msg = f"{a.cidr} removed — " + _set_profile(run, p, addrs, gw)
        return msg + ("\nNo static addresses left, so the port is back on DHCP." if not addrs else "")
    run(["ip", "addr", "del", a.cidr, "dev", p.name])
    return f"{a.cidr} removed" + (" (DHCP may hand it back at the next renewal)" if a.source == "dhcp" else "")


def do_gateway(run, p, form):
    gw, bypass = form["gw"], form["bypass"]
    if p.profile:
        name = _ensure_profile(run, p)
        cmd = ["nmcli", "connection", "modify", "id", name, "ipv4.never-default", "yes" if bypass else "no"]
        if p.profile.method == "manual":
            cmd += ["ipv4.gateway", gw]
        run(cmd)
        return "gateway " + _activate(run, p, name)
    run(["ip", "route", "del", "default", "dev", p.name], ok=(0, 2))
    if gw and not bypass:
        _live_gateway(run, p, gw)
    return f"default route via {gw}" if gw and not bypass else "default route removed"


def do_bypass(run, p, on, gw=""):
    if p.profile:
        name = _ensure_profile(run, p)
        run(["nmcli", "connection", "modify", "id", name, "ipv4.never-default", "yes" if on else "no"])
        return ("gateway bypassed — " if on else "gateway restored — ") + _activate(run, p, name)
    if on:
        run(["ip", "route", "del", "default", "dev", p.name], ok=(0, 2))
        return "gateway bypassed (temporary)"
    _live_gateway(run, p, gw)
    return f"default route via {gw} restored"


def do_dhcp(run, p):
    if not p.profile:
        raise CmdError(f"{p.name} isn't managed by NetworkManager, so DHCP can't be switched on here")
    return "DHCP " + _set_profile(run, p, [], "", never_default=False)


# ── Formatting ──────────────────────────────────────────────────────────────
def fmt_bits(bps):
    for unit in ("b/s", "kb/s", "Mb/s", "Gb/s"):
        if bps < 1000:
            return f"{bps:.0f} {unit}" if unit == "b/s" else f"{bps:.1f} {unit}"
        bps /= 1000
    return f"{bps:.1f} Tb/s"


def fmt_bytes(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def short_rate(bps):
    for unit in ("", "k", "M", "G"):
        if bps < 1000:
            return f"{bps:.0f}{unit}" if bps >= 10 or not unit else f"{bps:.1f}{unit}"
        bps /= 1000
    return f"{bps:.0f}T"


# fixed status colours (palette colours vary too much to carry meaning)
OK, WARN, BAD, TEMP = "#05ffa1", "#ffb000", "#ff1744", "#c77dff"

SOURCE_INFO = {
    "static":  (OK,     "saved"),
    "pending": (WARN,   "saved, not applied yet (no cable / other profile active)"),
    "dhcp":    (None,   "from DHCP"),          # palette accent
    "live":    (TEMP,   "temporary, gone when the port reconnects"),
}


# ── Widgets ─────────────────────────────────────────────────────────────────
class Banner(Widget):
    DEFAULT_CSS = "Banner { height: 2; }"

    def render(self):
        app = self.app
        w = self.size.width
        left = grad_text(" ▞▚ IPADD", app.grad, bold=True)
        left.append(" // ", style=app.shadow)
        left.append("CONTROL FREAKS NETWORK", style=f"bold {tint(app.shadow, 0.35)}")
        if DRY_RUN:
            left.append("  DRY RUN ", style=f"bold #000000 on {app.glitch}")
        user = getpass.getuser()
        right = Text()
        right.append(user, style=f"bold {app.grad.at(0)}")
        right.append("@", style=app.shadow)
        right.append(socket.gethostname(), style=f"bold {app.grad.at(1)}")
        right.append("  " + time.strftime("%H:%M:%S") + " ", style=tint(app.shadow, 0.3))
        if left.cell_len + right.cell_len + 2 > w:
            right = Text()
        line = left + Text(" " * max(1, w - left.cell_len - right.cell_len)) + right
        rule = Text("".join("━" for _ in range(w)))
        for i in range(w):
            rule.stylize(app.grad.at(i / max(1, w - 1)), i, i + 1)
        return Text("\n").join([line, rule])


class Section(Widget):
    DEFAULT_CSS = "Section { height: 1; margin-top: 1; }"

    def __init__(self, title, **kw):
        super().__init__(**kw)
        self.title = title
        self.extra = ""

    def render(self):
        app = self.app
        t = Text()
        t.append("▞▚ ", style=app.glitch)
        t.append(self.title, style=f"bold {app.shadow}")
        if self.extra:
            t.append("  " + self.extra, style=tint(app.shadow, 0.45))
        t.append(" ")
        n = max(0, self.size.width - t.cell_len)
        t.append_text(grad_text("─" * n, app.grad, t0=0.0, t1=0.6))
        return t


class PortCard(Widget):
    """Fetch-style key/value panel for the selected port."""
    DEFAULT_CSS = "PortCard { height: auto; }"
    KEY_W = 13

    def rows(self, p):
        app = self.app
        ok, bad, warn = OK, BAD, WARN
        if not p.admin_up:
            link = Text("● disabled (admin down)", style=bad)
        elif p.carrier is False:
            link = Text("● no cable / no link", style=warn)
        else:
            link = Text("● up", style=f"bold {ok}")
            link.append(f"   mtu {p.mtu}", style="dim")
        speed = f"{p.speed:,} Mb/s {p.duplex}".strip() if p.speed else "—"
        prof = p.profile
        if prof is None:
            profile = Text("not managed by NetworkManager", style="dim")
            mode = "live only (changes are temporary)"
        else:
            profile = Text(prof.name)
            if not prof.exists:
                profile.append("  (made on first change)", style="dim")
            elif prof.active:
                profile.append("  active", style=ok)
            else:
                profile.append("  inactive", style=warn)
                if p.nm_conn:
                    profile.append(f" — {p.nm_conn} has the port", style="dim")
            mode = {"auto": "DHCP", "manual": "static", "disabled": "IPv4 off",
                    "link-local": "link-local"}.get(prof.method, prof.method) if prof.exists else "DHCP (NetworkManager default)"
        gws = [g for g, _ in p.gateways if g]
        gw = Text(", ".join(gws) or (prof.gateway if prof and prof.gateway else "none"))
        if prof and prof.never_default:
            gw.append("  bypassed", style=f"bold {warn}")
        inet = app.internet
        net = Text(f"{inet[0]} → {inet[1]}" if inet else "no default route",
                   style=(f"bold {ok}" if inet and inet[0] == p.name else "") if inet else bad)
        return [
            (ICON.get(p.kind, ""), "PORT", Text(f"{p.name}  {p.kind}" + (f" · {p.driver}" if p.driver else ""))),
            (ICON["link"], "LINK", link),
            (ICON["speed"], "SPEED", Text(speed)),
            (ICON["mac"], "MAC", Text(p.mac or "—")),
            (ICON["profile"], "PROFILE", profile),
            (ICON["mode"], "IPv4", Text(mode)),
            (ICON["gateway"], "GATEWAY", gw),
            (ICON["internet"], "INTERNET", net),
        ] + [("⚠", "ROUTING", Text(f"{sub} actually goes out {dev}", style=f"bold {warn}"))
             for sub, dev in p.hijack]

    def line(self, i, n, icon, label, value, width):
        app = self.app
        color = app.grad.at(i / max(1, n - 1))
        t = Text(f" {icon}  {label}".ljust(self.KEY_W), style=f"bold {color}")
        v = value.copy()
        if not v.style and not v.spans:
            v.stylize(tint(color, 0.55))
        v.truncate(max(1, width - self.KEY_W), overflow="ellipsis")
        return t + v

    def render(self):
        p = self.app.current()
        if not p:
            return Text(" no network ports found", style="dim")
        rows = self.rows(p)
        n, w = len(rows), self.size.width
        if w < 84:
            return Text("\n").join(self.line(i, n, *r, w) for i, r in enumerate(rows))
        half, cw = (n + 1) // 2, (w - 2) // 2
        out = []
        for i in range(half):
            l = self.line(i, n, *rows[i], cw)
            l.pad_right(cw + 2 - l.cell_len)
            if i + half < n:
                l += self.line(i + half, n, *rows[i + half], cw)
            out.append(l)
        return Text("\n").join(out)


class Activity(Widget):
    DEFAULT_CSS = "Activity { height: auto; }"

    def render(self):
        app = self.app
        p = app.current()
        if not p:
            return Text()
        c = app.counters.get(p.name, {})
        rx, tx = app.rx_hist[p.name], app.tx_hist[p.name]
        t = Text()
        for arrow, label, hist, key, color in (("▼", "RX", rx, "rx", app.grad.at(0)),
                                                ("▲", "TX", tx, "tx", app.shadow)):
            t.append(f" {arrow} {label} ", style=f"bold {color}")
            t.append(f"{fmt_bits(hist[-1] if hist else 0):>10}", style=f"bold {tint(color, 0.4)}")
            t.append(f"  peak {fmt_bits(max(hist, default=0)):>10}", style="dim")
            t.append(f"  total {fmt_bytes(c.get(key + '_bytes', 0)):>9}", style="dim")
            t.append(f"  {c.get(key + '_packets', 0):,} pkts\n", style="dim")
        errs = c.get("rx_errors", 0) + c.get("tx_errors", 0)
        drops = c.get("rx_dropped", 0) + c.get("tx_dropped", 0)
        t.append(" ✖ errors ", style="dim")
        t.append(f"{errs:,}", style=app.theme_color("error") if errs else "dim")
        t.append("   ⊘ dropped ", style="dim")
        t.append(f"{drops:,}", style=WARN if drops else "dim")
        return t


class Legend(Widget):
    DEFAULT_CSS = "Legend { height: auto; }"

    def render(self):
        app = self.app
        t = Text(" ")
        for src, (color, _) in SOURCE_INFO.items():
            t.append("● ", style=app.source_color(src))
            t.append(f"{src}   ", style="dim")
        t.append("a", style=f"bold {app.shadow}")
        t.append(" add  ", style="dim")
        t.append("e", style=f"bold {app.shadow}")
        t.append(" edit  ", style="dim")
        t.append("d", style=f"bold {app.shadow}")
        t.append(" remove", style="dim")
        return t


# ── Dialogs ─────────────────────────────────────────────────────────────────
class Dialog(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def heading(self, text):
        return Static(grad_text(f"▞▚ {text}", self.app.grad, bold=True), classes="dlg-title")

    def action_cancel(self):
        self.dismiss(None)

    def fail(self, msg):
        self.query_one("#err", Static).update(Text(f"✖ {msg}", style=self.app.theme_color("error")))


class AddrDialog(Dialog):
    """Add a new address, or change `old` (an Addr) on port `p`."""

    def __init__(self, p, ports, old=None):
        super().__init__()
        self.p, self.ports, self.old = p, ports, old

    def compose(self):
        p, old = self.p, self.old
        prof = p.profile
        if old:
            ip, mask = str(old.iface.ip), str(old.iface.netmask)
        else:
            ip, mask = "", "255.255.255.0"
        gw = prof.gateway if prof and prof.method == "manual" else ""
        if not prof or (old and old.source in ("live", "dhcp")):
            gw = gw or next((g for g, _ in p.gateways if g), "")
        temp_forced = prof is None
        temp = temp_forced or bool(old and old.source in ("live", "dhcp"))
        with Vertical(id="dialog"):
            yield self.heading(f"{'CHANGE ' + old.cidr if old else 'ADD ADDRESS'} — {p.name}")
            yield Label("IP address")
            yield Input(ip, placeholder="192.168.1.50   or   192.168.1.50/24", id="ip")
            yield Label("Subnet mask")
            yield Input(mask, placeholder="255.255.255.0   or   24", id="mask")
            yield Label("Gateway  (optional — blank for none)")
            yield Input(gw, placeholder="192.168.1.1", id="gw")
            yield Checkbox("Temporary (plain `ip`, lost when the port reconnects)", temp, id="temp",
                           disabled=temp_forced or bool(old))
            yield Static(id="calc")
            yield Static(id="err")
            with Horizontal(id="buttons"):
                yield Button("Apply", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.update_calc()

    @on(Input.Changed)
    def update_calc(self, event=None):
        self.query_one("#err", Static).update("")
        calc = self.query_one("#calc", Static)
        try:
            iface = parse_address(self.query_one("#ip", Input).value, self.query_one("#mask", Input).value)
        except ValueError:
            calc.update("")
            return
        net, mask, hosts, bcast = subnet_summary(iface)
        app = self.app
        t = Text()
        for k, v in (("network", f"{net}"), ("mask", f"{mask}"), ("hosts", hosts), ("broadcast", f"{bcast}")):
            t.append(f"  {k:<10}", style=f"bold {app.shadow}")
            t.append(v + "\n", style=tint(app.shadow, 0.5))
        for sub, dev, table in competing_routes(net, self.p.name):
            why = "Tailscale (accept-routes) will win over this port" if table == "52" \
                else "traffic may leave the wrong port"
            t.append(f"  ⚠ {sub} is already routed out {dev} — {why}\n", style=f"bold {WARN}")
        calc.update(t)

    @on(Input.Submitted)
    def submitted(self, event):
        order = ["ip", "mask", "gw"]
        i = order.index(event.input.id)
        if i < len(order) - 1:
            self.query_one(f"#{order[i + 1]}", Input).focus()
        else:
            self.submit()

    @on(Button.Pressed, "#ok")
    def submit(self):
        p, old = self.p, self.old
        try:
            iface = parse_address(self.query_one("#ip", Input).value, self.query_one("#mask", Input).value)
            others = [a for a in p.addrs if not old or a.cidr != old.cidr]
            if any(a.iface.ip == iface.ip for a in others):
                raise ValueError(f"{iface.ip} is already on {p.name}")
            temp = self.query_one("#temp", Checkbox).value
            # the gateway has to be reachable from an address the port will actually have
            nets = [iface.network] + [a.iface.network for a in others
                                      if temp or not p.profile or a.source in ("static", "pending")]
            gw = parse_gateway(self.query_one("#gw", Input).value, nets, [iface.ip])
        except ValueError as e:
            self.fail(e)
            return
        self.dismiss({"cidr": str(iface), "gw": gw, "temp": temp})

    @on(Button.Pressed, "#cancel")
    def cancel(self):
        self.dismiss(None)


class GatewayDialog(Dialog):
    def __init__(self, p):
        super().__init__()
        self.p = p

    def compose(self):
        p, prof = self.p, self.p.profile
        dhcp = prof is not None and prof.method != "manual"
        gw = prof.gateway if prof and prof.gateway else next((g for g, _ in p.gateways if g), "")
        with Vertical(id="dialog"):
            yield self.heading(f"GATEWAY — {p.name}")
            yield Label("Gateway" + ("  (comes from DHCP on this port)" if dhcp else "  (blank for none)"))
            yield Input(gw, placeholder="192.168.1.1", id="gw", disabled=dhcp)
            yield Checkbox("Bypass — don't send internet traffic through this port's gateway",
                           bool(prof and prof.never_default), id="bypass")
            yield Static(Text("  Bypass keeps internet on wifi while the cable talks to the field network.",
                              style="dim"))
            yield Static(id="err")
            with Horizontal(id="buttons"):
                yield Button("Apply", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def submit(self):
        p = self.p
        try:
            nets = [a.iface.network for a in p.addrs]
            gw = parse_gateway(self.query_one("#gw", Input).value, nets, [a.iface.ip for a in p.addrs])
        except ValueError as e:
            self.fail(e)
            return
        self.dismiss({"gw": gw, "bypass": self.query_one("#bypass", Checkbox).value})

    @on(Button.Pressed, "#cancel")
    def cancel(self):
        self.dismiss(None)


class ConfirmDialog(Dialog):
    BINDINGS = [Binding("y", "yes", "yes"), Binding("n,escape", "cancel", "no")]

    def __init__(self, title, body):
        super().__init__()
        self.title_text, self.body = title, body

    def compose(self):
        with Vertical(id="dialog"):
            yield self.heading(self.title_text)
            yield Static(self.body)
            with Horizontal(id="buttons"):
                yield Button("Yes  (y)", variant="error", id="yes")
                yield Button("No  (n)", id="no")

    @on(Button.Pressed, "#yes")
    def action_yes(self):
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def no(self):
        self.dismiss(None)


class PingDialog(Dialog):
    def __init__(self, p, target):
        super().__init__()
        self.p, self.target = p, target

    def compose(self):
        with Vertical(id="dialog", classes="wide"):
            yield self.heading("PING")
            yield Label("Target  (enter to ping, esc to close)")
            yield Input(self.target, placeholder="192.168.1.1", id="target")
            yield Static(id="err")
            yield Log(id="out", auto_scroll=True)

    @on(Input.Submitted)
    def go(self):
        target = self.query_one("#target", Input).value.strip()
        try:
            ipaddress.IPv4Address(target)
        except ValueError:
            self.fail(f"{target or 'that'} isn't an IPv4 address")
            return
        self.query_one("#err", Static).update("")
        log = self.query_one("#out", Log)
        log.clear()
        self.ping(target, log)

    @work(thread=True, exclusive=True)
    def ping(self, target, log):
        route = sh_json(["ip", "-j", "route", "get", target])
        if route:
            r = route[0]
            via = f" via {r['gateway']}" if r.get("gateway") else ""
            note = "" if r.get("dev") == self.p.name else f"   ⚠ not {self.p.name}!"
            self.app.call_from_thread(log.write_line, f"route: out {r.get('dev')}{via} from {r.get('prefsrc', '?')}{note}")
        try:
            proc = subprocess.Popen(["ping", "-c", "4", "-W", "1", target], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        except OSError as e:
            self.app.call_from_thread(log.write_line, str(e))
            return
        for line in proc.stdout:
            self.app.call_from_thread(log.write_line, line.rstrip())
        proc.wait()


# ── App ─────────────────────────────────────────────────────────────────────
CSS = """
Screen { background: ansi_default; }
#body { height: 1fr; }
#ports {
    width: 32; background: ansi_default; border: round $secondary 70%;
    border-title-color: $accent; border-title-style: bold; padding: 0;
}
#ports > .option-list--option-highlighted { background: $primary 20%; }
#ports:focus > .option-list--option-highlighted { background: $primary 30%; }
#detail {
    width: 1fr; background: ansi_default; border: round $primary 70%;
    border-title-style: bold; padding: 0 1; scrollbar-size-vertical: 1;
}
DataTable { height: auto; max-height: 12; background: ansi_default; margin-top: 1; scrollbar-size-horizontal: 0; }
DataTable > .datatable--header { background: ansi_default; color: $accent; text-style: bold; }
DataTable > .datatable--cursor { background: $primary 15%; }
DataTable:focus > .datatable--cursor { background: $primary 35%; }
.empty { color: $text-muted; margin-top: 1; }
.spark-row { height: 3; margin-top: 1; }
.spark-label { width: 6; color: $accent; text-style: bold; padding-top: 1; }
Sparkline { height: 3; }
#rx > .sparkline--min-color { color: $primary 30%; }
#rx > .sparkline--max-color { color: $primary; }
#tx > .sparkline--min-color { color: $accent 30%; }
#tx > .sparkline--max-color { color: $accent; }
Footer { background: ansi_default; }
Footer FooterKey { background: ansi_default; }
Footer .footer-key--key { color: $accent; background: ansi_default; text-style: bold; }

Dialog { align: center middle; background: $background 50%; }
#dialog {
    width: 72; height: auto; max-height: 90%; padding: 1 2;
    border: heavy $accent; background: $surface;
}
#dialog.wide { width: 90; }
#dialog Label { color: $accent; margin-top: 1; }
#dialog Input { border: tall $primary 40%; }
#dialog Input:focus { border: tall $primary; }
#dialog Checkbox { margin-top: 1; background: $surface; border: none; }
#dialog Log { scrollbar-size-horizontal: 0; height: 12; margin-top: 1; border: round $primary 40%; background: $surface; }
#calc { margin-top: 1; }
#buttons { height: auto; align-horizontal: right; margin-top: 1; }
#buttons Button { margin-left: 2; }
"""


APP_ACTIONS = {"add", "edit", "remove", "gateway", "bypass", "dhcp", "ping", "palette", "refresh", "quit", "pick"}


class IpAdd(App):
    CSS = CSS
    TITLE = "ipadd"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("a", "add", "add ip"),
        Binding("e", "edit", "edit"),
        Binding("d,delete", "remove", "remove"),
        Binding("g", "gateway", "gateway"),
        Binding("b", "bypass", "bypass gw"),
        Binding("h", "dhcp", "dhcp"),
        Binding("x", "ping", "ping"),
        Binding("p", "palette", "palette"),
        Binding("r", "refresh", "refresh", show=False),
        Binding("q", "quit", "quit"),
    ] + [Binding(str(i), f"pick({i - 1})", show=False) for i in range(1, 10)]

    def __init__(self, want_port=None, theme=None):
        super().__init__()
        self.palettes = load_palettes()
        names = list(self.palettes)
        self.pal_name = theme if theme in self.palettes else fetch_palette(names)
        self.want = want_port
        self.ports, self.internet, self.sel = [], None, None
        self.counters, self.prev = {}, {}
        self.rx_hist = collections.defaultdict(lambda: collections.deque([0.0] * HIST, maxlen=HIST))
        self.tx_hist = collections.defaultdict(lambda: collections.deque([0.0] * HIST, maxlen=HIST))
        self.ticks = 0
        self.live_gw = {}           # port -> gateway removed by a live bypass, for restoring
        self.shown = {}             # widget id -> last rows rendered (skip redundant rebuilds)

    # palette ----------------------------------------------------------------
    @property
    def pal(self):
        return self.palettes[self.pal_name]

    @property
    def grad(self):
        return Gradient(self.pal["fill"])

    @property
    def shadow(self):
        return self.pal["shadow"]

    @property
    def glitch(self):
        return self.pal["glitch"]

    def theme_color(self, name):
        return {"success": OK, "error": BAD, "warning": WARN}[name]

    def source_color(self, src):
        return SOURCE_INFO[src][0] or self.shadow

    def theme_name(self, name):
        return "cf-" + name.lower().replace(" ", "-")

    def register_palettes(self):
        for name, pal in self.palettes.items():
            self.register_theme(Theme(
                name=self.theme_name(name), primary=pal["fill"][0], secondary=pal["fill"][-1],
                accent=pal["shadow"], warning=WARN, error=BAD, success=OK,
                foreground="#e6e6f0", background="#0a0a12", surface="#12121e", panel="#1a1a2a", dark=True))

    # layout -----------------------------------------------------------------
    def compose(self):
        yield Banner()
        with Horizontal(id="body"):
            yield OptionList(id="ports")
            with VerticalScroll(id="detail"):
                yield PortCard()
                yield Section("ADDRESSES", id="sec-addr")
                yield DataTable(id="addrs", cursor_type="row", zebra_stripes=False)
                yield Static(Text(" no IPv4 addresses — press a to add one"), id="addrs-empty", classes="empty")
                yield Legend()
                yield Section("ACTIVITY", id="sec-act")
                with Horizontal(classes="spark-row"):
                    yield Static("▼ RX", classes="spark-label")
                    yield Sparkline([0.0] * HIST, id="rx")
                with Horizontal(classes="spark-row"):
                    yield Static("▲ TX", classes="spark-label")
                    yield Sparkline([0.0] * HIST, id="tx")
                yield Activity()
                yield Section("NEIGHBOURS", id="sec-neigh")
                yield DataTable(id="neigh", cursor_type="row")
                yield Static(Text(" nothing seen on this port yet — x pings something to wake it up"),
                             id="neigh-empty", classes="empty")
        yield Footer()

    def on_mount(self):
        self.register_palettes()
        self.theme = self.theme_name(self.pal_name)
        self.query_one("#addrs", DataTable).add_columns(" ", "ADDRESS", "SUBNET MASK", "NETWORK", "SOURCE")
        self.query_one("#neigh", DataTable).add_columns(" ", "IP", "MAC", "STATE", "VENDOR")
        self.query_one("#ports").border_title = "PORTS"
        self.query_one("#detail").can_focus = False
        self.load_vendors()
        self.sample()
        self.apply_scan(*scan())
        self.set_interval(1.0, self.tick)
        self.query_one("#ports").focus()

    @work(thread=True)
    def load_vendors(self):
        load_oui()
        self.call_from_thread(self.refresh_detail, True)

    def check_action(self, action, parameters):
        # our single-letter keys stay quiet while a dialog is open
        return not (action in APP_ACTIONS and isinstance(self.screen, ModalScreen))

    # data -------------------------------------------------------------------
    def current(self):
        return next((p for p in self.ports if p.name == self.sel), None)

    def sample(self):
        now = time.monotonic()
        for name in os.listdir("/sys/class/net"):
            c = read_counters(name)
            if name in self.prev:
                t0, c0 = self.prev[name]
                dt = max(1e-3, now - t0)
                self.rx_hist[name].append(max(0, c["rx_bytes"] - c0["rx_bytes"]) * 8 / dt)
                self.tx_hist[name].append(max(0, c["tx_bytes"] - c0["tx_bytes"]) * 8 / dt)
            self.prev[name] = (now, c)
            self.counters[name] = c

    def tick(self):
        self.ticks += 1
        self.sample()
        self.query_one(Banner).refresh()
        self.update_port_list()
        self.update_activity()
        if self.ticks % SCAN_EVERY == 0:
            self.rescan()

    @work(thread=True, exclusive=True, group="scan")
    def rescan(self):
        ports, internet = scan()
        self.call_from_thread(self.apply_scan, ports, internet)

    def apply_scan(self, ports, internet):
        self.ports, self.internet = ports, internet
        names = [p.name for p in ports]
        if self.sel not in names:
            want = self.want if self.want in names else None
            pick = want or next((p.name for p in ports if p.kind == "ethernet" and p.carrier), None) \
                or next((p.name for p in ports if p.kind == "ethernet"), None) \
                or next((p.name for p in ports if p.kind != "loopback"), None) or (names[0] if names else None)
            self.sel = pick
        self.update_port_list()
        self.refresh_detail()

    # rendering --------------------------------------------------------------
    def port_prompt(self, i, p):
        grad = self.grad
        color = grad.at(i / max(1, len(self.ports) - 1))
        ok = self.theme_color("success")
        if not p.admin_up:
            dot, state = self.theme_color("error"), "off"
        elif p.carrier is False:
            dot, state = WARN, "no link"
        else:
            dot, state = ok, "up"
        t = Text()
        t.append(f" {i + 1} " if i < 9 else "   ", style="dim")
        t.append(f"{ICON.get(p.kind, '')}  ", style=color)
        t.append(p.name, style=f"bold {color}")
        right = Text(f"● {state} ", style=dot)
        t.append(" " * max(1, 28 - t.cell_len - right.cell_len))
        t.append_text(right)
        v4 = [a for a in p.addrs if a.source != "pending"]
        sub = Text("      ")
        sub.append(v4[0].cidr if v4 else "no address", style=tint(color, 0.5) if v4 else "dim")
        if len(v4) > 1:
            sub.append(f" +{len(v4) - 1}", style=self.shadow)
        rx, tx = self.rx_hist[p.name][-1], self.tx_hist[p.name][-1]
        act = Text("      ")
        act.append(f"▼ {short_rate(rx):>5}", style=grad.at(0) if rx > 0 else "dim")
        act.append("  ")
        act.append(f"▲ {short_rate(tx):>5}", style=self.shadow if tx > 0 else "dim")
        return Text("\n").join([t, sub, act])

    def update_port_list(self):
        ol = self.query_one("#ports", OptionList)
        ids = [p.name for p in self.ports]
        if [o.id for o in ol.options] != ids:
            ol.clear_options()
            ol.add_options([Option(self.port_prompt(i, p), id=p.name) for i, p in enumerate(self.ports)])
            if self.sel in ids:
                ol.highlighted = ids.index(self.sel)
        else:
            for i, p in enumerate(self.ports):
                ol.replace_option_prompt(p.name, self.port_prompt(i, p))

    def update_activity(self):
        p = self.current()
        if not p:
            return
        self.query_one("#rx", Sparkline).data = list(self.rx_hist[p.name])
        self.query_one("#tx", Sparkline).data = list(self.tx_hist[p.name])
        self.query_one(Activity).refresh()

    def fill_table(self, tid, rows, force=False):
        """Rebuild a table only when its rows changed, keeping the cursor on the same row."""
        table = self.query_one(f"#{tid}", DataTable)
        if not force and self.shown.get(tid) == rows:
            return
        self.shown[tid] = rows
        key = None
        if table.row_count:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        table.clear()
        for k, cells in rows:
            table.add_row(*cells, key=k)
        if key in [k for k, _ in rows]:
            table.move_cursor(row=[k for k, _ in rows].index(key))
        self.query_one(f"#{tid}-empty").display = not rows
        table.display = bool(rows)

    def refresh_detail(self, force=False):
        p = self.current()
        detail = self.query_one("#detail")
        if not p:
            detail.border_title = "NO PORTS"
            return
        detail.border_title = f"{ICON.get(p.kind, '')} {p.name}"
        detail.border_subtitle = (f"profile {p.profile.name}" if p.profile and p.profile.exists
                                  else "live" if not p.profile else "")
        self.query_one(PortCard).refresh(layout=True)
        self.query_one(Legend).refresh()
        rows = []
        for a in p.addrs:
            i = a.iface
            c = self.source_color(a.source)
            rows.append((a.cidr, (Text("●", style=c), Text(str(i), style="bold"), str(i.netmask),
                                  str(i.network), Text(a.source, style=c))))
        self.fill_table("addrs", rows, force)
        gws = {g for g, _ in p.gateways}
        rows = []
        for ip, mac, state in sorted(p.neigh, key=lambda n: ipaddress.IPv4Address(n[0])):
            gw = ip in gws
            rows.append((ip, (Text("◆" if gw else "●", style=self.shadow if gw else self.grad.at(0.3)),
                              Text(ip, style="bold"), mac,
                              Text("gateway" if gw else state, style=self.shadow if gw else "dim"),
                              Text(vendor(mac)[:30], style="dim"))))
        self.fill_table("neigh", rows, force)
        for sid in ("sec-addr", "sec-act", "sec-neigh"):
            self.query_one(f"#{sid}").refresh()
        self.query_one("#sec-neigh", Section).extra = f"{len(p.neigh)} seen" if p.neigh else ""
        self.update_activity()

    # events -----------------------------------------------------------------
    @on(OptionList.OptionHighlighted, "#ports")
    def port_highlighted(self, event):
        if event.option.id != self.sel:
            self.sel = event.option.id
            self.query_one("#detail").scroll_home(animate=False)
            self.refresh_detail(force=True)

    @on(OptionList.OptionSelected, "#ports")
    def port_selected(self, event):
        self.query_one("#addrs", DataTable).focus()

    @on(DataTable.RowSelected, "#addrs")
    def addr_selected(self, event):
        self.action_edit()

    @on(DataTable.RowSelected, "#neigh")
    def neigh_selected(self, event):
        p = self.current()
        if p:
            self.push_screen(PingDialog(p, event.row_key.value))

    # actions ----------------------------------------------------------------
    def action_pick(self, i):
        if i < len(self.ports):
            self.query_one("#ports", OptionList).highlighted = i

    def action_refresh(self):
        self.rescan()

    def action_palette(self):
        names = list(self.palettes)
        self.pal_name = names[(names.index(self.pal_name) + 1) % len(names)]
        self.theme = self.theme_name(self.pal_name)
        self.notify(self.pal_name, title="palette", timeout=2)
        self.query_one(Banner).refresh()
        self.update_port_list()
        self.refresh_detail(force=True)

    def selected_addr(self):
        p = self.current()
        table = self.query_one("#addrs", DataTable)
        if not p or not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return next((a for a in p.addrs if a.cidr == key), None)

    def editable(self):
        p = self.current()
        if not p:
            return None
        if p.kind == "loopback":
            self.notify("lo is the loopback port — leave it be", severity="warning")
            return None
        return p

    def action_add(self):
        p = self.editable()
        if p:
            self.push_screen(AddrDialog(p, self.ports),
                             lambda r: r and self.change(f"add {r['cidr']}", do_add, p, r))

    def action_edit(self):
        p = self.editable()
        a = self.selected_addr()
        if not p:
            return
        if not a:
            self.notify("pick an address in the table first (tab to it)", severity="warning")
            return
        self.push_screen(AddrDialog(p, self.ports, a),
                         lambda r: r and self.change(f"change {a.cidr}", do_edit, p, a, r))

    def action_remove(self):
        p = self.editable()
        a = self.selected_addr()
        if not p:
            return
        if not a:
            self.notify("pick an address in the table first (tab to it)", severity="warning")
            return
        body = Text(f"\nRemove {a.cidr} from {p.name}?\n")
        body.append(f"({SOURCE_INFO[a.source][1]})", style="dim")
        self.push_screen(ConfirmDialog("REMOVE ADDRESS", body),
                         lambda ok: ok and self.change(f"remove {a.cidr}", do_remove, p, a))

    def action_gateway(self):
        p = self.editable()
        if p:
            self.push_screen(GatewayDialog(p), lambda r: r is not None and self.change("gateway", do_gateway, p, r))

    def action_bypass(self):
        p = self.editable()
        if not p:
            return
        if p.profile:
            on_ = not p.profile.never_default
            self.change("bypass gateway" if on_ else "restore gateway", do_bypass, p, on_)
            return
        gw = next((g for g, _ in p.gateways if g), "")
        if gw:
            self.live_gw[p.name] = gw
            self.change("bypass gateway", do_bypass, p, True)
        elif self.live_gw.get(p.name):
            self.change("restore gateway", do_bypass, p, False, self.live_gw.pop(p.name))
        else:
            self.notify(f"{p.name} has no default route to bypass — set one with g", severity="warning")

    def action_dhcp(self):
        p = self.editable()
        if not p:
            return
        body = Text(f"\nPut {p.name} back on DHCP?\n")
        body.append("Static addresses and the gateway saved for this port are cleared.", style="dim")
        self.push_screen(ConfirmDialog("DHCP", body), lambda ok: ok and self.change("dhcp", do_dhcp, p))

    def action_ping(self):
        p = self.current()
        if not p:
            return
        target = next((g for g, _ in p.gateways if g), "") or (p.profile.gateway if p.profile else "")
        table = self.query_one("#neigh", DataTable)
        if not target and table.row_count:
            target = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        self.push_screen(PingDialog(p, target))

    # applying changes -------------------------------------------------------
    def ensure_sudo(self):
        if IS_ROOT or DRY_RUN:
            return True
        if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0:
            return True
        try:
            with self.suspend():
                print("\n  \x1b[1;38;2;%d;%d;%dm▞▚ ipadd\x1b[0m needs sudo to change network settings\n" % hex_rgb(self.shadow))
                ok = subprocess.run(["sudo", "-v", "-p", "  password for %u: "]).returncode == 0
        except Exception:
            return False
        return ok

    def change(self, what, fn, *args):
        if not self.ensure_sudo():
            self.notify("sudo wasn't unlocked, nothing changed", title=what, severity="error")
            return
        self.notify("working…", title=what, timeout=2)
        self.run_change(what, fn, args)

    @work(thread=True, group="change")
    def run_change(self, what, fn, args):
        run = Runner()
        try:
            msg = fn(run, *args)
            if DRY_RUN:
                msg = "would run:\n" + "\n".join(run.cmds)
            self.call_from_thread(self.notify, msg, title=what, timeout=8 if DRY_RUN else 5)
        except CmdError as e:
            self.call_from_thread(self.notify, str(e), title=f"{what} failed", severity="error", timeout=10)
        finally:
            time.sleep(0.5)
            self.call_from_thread(self.rescan)


def main(argv):
    global DRY_RUN
    port = theme = None
    args = iter(argv)
    for a in args:
        if a in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        if a in ("-n", "--dry-run"):
            DRY_RUN = True
        elif a == "--theme":
            theme = next(args, "").upper()
        elif not a.startswith("-"):
            port = a
    if not sys.stdout.isatty():
        print("ipadd needs a terminal", file=sys.stderr)
        return 1
    IpAdd(port, theme).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
