"""Windows networking helpers (netsh, ping, DNS, firewall, Native Wi-Fi API).

Everything acts only on THIS computer: it reads what Windows already knows,
measures this machine's own connection, and changes only this machine's own
adapter, firewall and Wi-Fi association. Parsers are pure functions so they
can be tested on any OS.
"""
from __future__ import annotations

import ctypes
import platform
import random
import re
import socket
import struct
import subprocess
import time

IS_WINDOWS = platform.system() == "Windows"
_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
LOCATION_HINT = ("Windows hides Wi-Fi details while Location is off. Turn on Settings > "
                 "Privacy & security > Location, and 'Let desktop apps access your location'.")
_state = {"location_blocked": False}


def run(args, timeout=20):
    """Run a command; returns (output, returncode). Never raises."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
        return (p.stdout or "") + (p.stderr or ""), p.returncode
    except (OSError, subprocess.SubprocessError, ValueError):
        return "", 1


def _q(s):
    return str(s or "").replace('"', "")


def is_admin():
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def location_blocked():
    return _state["location_blocked"]


# ------------------------------------------------------------------ parsers
def _pct(s):
    m = re.search(r"(\d+)\s*%", s or "")
    return int(m.group(1)) if m else None


def _num(s):
    m = re.search(r"-?[\d.]+", s or "")
    return float(m.group(0)) if m else None


def parse_interfaces(text):
    blocks, cur = [], {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        key, _, val = raw.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "name":
            if cur:
                blocks.append(cur)
            cur = {}
        cur[key] = val
    if cur:
        blocks.append(cur)
    out = []
    for b in blocks:
        out.append({
            "name": b.get("name", ""), "description": b.get("description", ""),
            "state": b.get("state", "").lower(), "ssid": b.get("ssid", ""),
            "profile": b.get("profile", "") or b.get("ssid", ""),
            "bssid": b.get("ap bssid", b.get("bssid", "")).lower(),
            "band": b.get("band", ""), "channel": b.get("channel", ""),
            "radio": b.get("radio type", ""), "signal": _pct(b.get("signal", "")),
            "rx_mbps": _num(b.get("receive rate (mbps)", "")),
            "tx_mbps": _num(b.get("transmit rate (mbps)", "")),
        })
    return out


def parse_networks(text, want_ssid=None):
    aps, ssid, pending = [], None, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^SSID\s+\d+\s*:\s*(.*)$", line)
        if m:
            if pending:
                aps.append(pending); pending = None
            ssid = m.group(1).strip(); continue
        m = re.match(r"^BSSID\s+\d+\s*:\s*(.*)$", line)
        if m:
            if pending:
                aps.append(pending)
            pending = {"ssid": ssid, "bssid": m.group(1).strip().lower(), "signal": None,
                       "band": None, "channel": None, "channel_util": None}
            continue
        if pending is None:
            continue
        key, sep, val = line.partition(":")
        if not sep:
            continue
        key, val = key.strip().lower(), val.strip()
        if key == "signal":
            pending["signal"] = _pct(val)
        elif key == "band":
            pending["band"] = val
        elif key == "channel":
            pending["channel"] = val
        elif key == "channel utilization":
            mu = re.search(r"\((\d+)\s*%\)", val)
            if mu:
                pending["channel_util"] = int(mu.group(1))
    if pending:
        aps.append(pending)
    if want_ssid:
        aps = [a for a in aps if a["ssid"] == want_ssid]
    return aps


_TIME_RE = re.compile(r"(?:time|Zeit|temps|tiempo|tempo)\s*[=<]\s*([\d.,]+)\s*ms", re.I)


def parse_ping_once(text):
    m = _TIME_RE.search(text)
    if not m:
        return None
    return max(0.5, float(m.group(1).replace(",", ".")))


def parse_ping_summary(text):
    loss, avg = 100.0, None
    m = re.search(r"\(([\d.]+)%\s*loss\)", text) or re.search(r"([\d.]+)%\s*packet loss", text)
    if m:
        loss = float(m.group(1))
    m = re.search(r"Average\s*=\s*([\d.]+)\s*ms", text) or re.search(r"=\s*[\d.]+/([\d.]+)/", text)
    if m:
        avg = float(m.group(1))
    return {"loss": loss, "latency": avg}


# ------------------------------------------------------------------ readers
def _netsh_wlan(*args):
    text, rc = run(["netsh", "wlan", *args])
    low = text.lower()
    _state["location_blocked"] = "location" in low and ("permission" in low or "services" in low)
    return text


def interfaces():
    return parse_interfaces(_netsh_wlan("show", "interfaces"))


def current_interface():
    ifs = interfaces()
    for i in ifs:
        if i["state"] == "connected":
            return i
    return ifs[0] if ifs else None


def scan(ssid=None):
    return parse_networks(_netsh_wlan("show", "networks", "mode=bssid"), ssid)


def ping_once(host, timeout_ms=1000):
    if not host:
        return None
    args = (["ping", "-n", "1", "-w", str(timeout_ms), host] if IS_WINDOWS
            else ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host])
    text, _ = run(args, timeout=timeout_ms / 1000 + 3)
    return parse_ping_once(text)


def ping_summary(host, count=6):
    args = (["ping", "-n", str(count), "-w", "1000", host] if IS_WINDOWS
            else ["ping", "-c", str(count), "-W", "1", host])
    text, _ = run(args, timeout=count * 2 + 5)
    return parse_ping_summary(text)


def dns_query(server, name="www.google.com", timeout=1.5):
    """One real DNS A query over UDP. Returns ms on a valid answer, else None."""
    if not server:
        return None
    qid = random.randint(0, 0xFFFF)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for part in name.split("."):
        q += bytes([len(part)]) + part.encode()
    q += b"\x00" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    t0 = time.perf_counter()
    try:
        s.sendto(q, (server, 53))
        while True:
            data, _ = s.recvfrom(2048)
            if len(data) >= 4 and struct.unpack(">H", data[:2])[0] == qid:
                return round((time.perf_counter() - t0) * 1000, 1) if (data[3] & 0x0F) == 0 else None
    except OSError:
        return None
    finally:
        s.close()


def default_gateway():
    if IS_WINDOWS:
        text, _ = run(["route", "print", "-4", "0.0.0.0"])
        best = None
        for line in text.splitlines():
            p = line.split()
            if len(p) >= 5 and p[0] == "0.0.0.0" and p[1] == "0.0.0.0" \
                    and re.match(r"^\d+\.\d+\.\d+\.\d+$", p[2]) and p[4].isdigit():
                if best is None or int(p[4]) < best[1]:
                    best = (p[2], int(p[4]))
        return best[0] if best else None
    text, _ = run(["ip", "route", "show", "default"])
    m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", text)
    return m.group(1) if m else None


def dns_servers(alias=None):
    if IS_WINDOWS:
        ps = ("Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object {$_.ServerAddresses} | "
              "ForEach-Object { $_.InterfaceAlias + '|' + ($_.ServerAddresses -join ',') }")
        text, _ = run(["powershell", "-NoProfile", "-Command", ps], timeout=15)
        rows = [l.split("|", 1) for l in text.splitlines() if "|" in l]
        for a, s in rows:
            if alias and a.strip().lower() == alias.lower():
                return [x for x in s.split(",") if x]
        for a, s in rows:
            if "loopback" not in a.lower():
                return [x for x in s.split(",") if x]
        return []
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as f:
            return re.findall(r"^nameserver\s+(\d+\.\d+\.\d+\.\d+)", f.read(), re.M)
    except OSError:
        return []


def host_load():
    try:
        import psutil
        return float(psutil.cpu_percent(interval=None))
    except Exception:  # noqa: BLE001
        return 0.0


def nic_bytes(name):
    try:
        import psutil
        c = psutil.net_io_counters(pernic=True).get(name)
        return (c.bytes_recv + c.bytes_sent) if c else None
    except Exception:  # noqa: BLE001
        return None


def adapter_enabled(name):
    """True/False if Windows knows the adapter's admin state, None if unknown."""
    if not IS_WINDOWS:
        return None
    text, _ = run(["netsh", "interface", "show", "interface", f"name={name}"])
    m = re.search(r"Administrative state:\s*(\w+)", text, re.I)
    if not m:
        return None
    return m.group(1).lower().startswith("enab")


# ------------------------------------------------------------------ actions
def set_interface(name, enabled=True):
    cmd = f'netsh interface set interface name="{_q(name)}" admin={"enabled" if enabled else "disabled"}'
    text, rc = run(cmd if IS_WINDOWS else cmd.split())
    return {"ok": rc == 0, "output": text.strip()}


def disconnect(name=None):
    cmd = "netsh wlan disconnect" + (f' interface="{_q(name)}"' if name else "")
    text, rc = run(cmd if IS_WINDOWS else cmd.split())
    return {"ok": rc == 0, "output": text.strip()}


def reconnect(profile, ssid=None, name=None):
    cmd = f'netsh wlan connect name="{_q(profile)}"'
    if ssid:
        cmd += f' ssid="{_q(ssid)}"'
    if name:
        cmd += f' interface="{_q(name)}"'
    text, rc = run(cmd if IS_WINDOWS else cmd.split())
    return {"ok": rc == 0, "output": text.strip()}


def flush_dns():
    text, rc = run(["ipconfig", "/flushdns"])
    return {"ok": rc == 0, "output": text.strip()}


def firewall(cmd):
    if not IS_WINDOWS:
        return False, "Windows only"
    text, rc = run("netsh advfirewall firewall " + cmd, timeout=15)
    return rc == 0, text.strip()


def find_rules(names):
    """Host policy audit: which of the given firewall rule names exist right now."""
    if not IS_WINDOWS:
        return []
    found = []
    for n in names:
        text, rc = run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={n}"], timeout=10)
        if rc == 0 and "no rules match" not in text.lower() and n.lower() in text.lower():
            found.append(n)
    return found


# ------------------------------------------------- Native Wi-Fi: pin a BSSID
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class _DOT11_SSID(ctypes.Structure):
    _fields_ = [("uSSIDLength", ctypes.c_ulong), ("ucSSID", ctypes.c_ubyte * 32)]


class _NDIS_HDR(ctypes.Structure):
    _fields_ = [("Type", ctypes.c_ubyte), ("Revision", ctypes.c_ubyte), ("Size", ctypes.c_ushort)]


class _BSSID_LIST(ctypes.Structure):
    _fields_ = [("Header", _NDIS_HDR), ("uNumOfEntries", ctypes.c_ulong),
                ("uTotalNumOfEntries", ctypes.c_ulong), ("BSSIDs", (ctypes.c_ubyte * 6) * 1)]


class _CONN_PARAMS(ctypes.Structure):
    _fields_ = [("wlanConnectionMode", ctypes.c_int), ("strProfile", ctypes.c_wchar_p),
                ("pDot11Ssid", ctypes.POINTER(_DOT11_SSID)),
                ("pDesiredBssidList", ctypes.POINTER(_BSSID_LIST)),
                ("dot11BssType", ctypes.c_int), ("dwFlags", ctypes.c_ulong)]


class _IF_INFO(ctypes.Structure):
    _fields_ = [("InterfaceGuid", _GUID), ("strInterfaceDescription", ctypes.c_wchar * 256),
                ("isState", ctypes.c_int)]


class _IF_LIST(ctypes.Structure):
    _fields_ = [("dwNumberOfItems", ctypes.c_ulong), ("dwIndex", ctypes.c_ulong),
                ("InterfaceInfo", _IF_INFO * 1)]


def _mac(bssid):
    parts = re.split(r"[:\-]", bssid.strip())
    if len(parts) != 6:
        raise ValueError(f"bad BSSID {bssid!r}")
    return bytes(int(p, 16) for p in parts)


def connect_bssid(profile, bssid):
    """Ask this PC's radio to join saved profile `profile` on exactly `bssid`."""
    if not IS_WINDOWS:
        return {"ok": False, "error": "Windows only"}
    try:
        wlan = ctypes.windll.wlanapi
        mac = _mac(bssid)
    except (AttributeError, OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    wlan.WlanOpenHandle.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
                                    ctypes.POINTER(ctypes.c_void_p)]
    wlan.WlanEnumInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.POINTER(_IF_LIST))]
    wlan.WlanConnect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GUID),
                                 ctypes.POINTER(_CONN_PARAMS), ctypes.c_void_p]
    wlan.WlanCloseHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    wlan.WlanFreeMemory.argtypes = [ctypes.c_void_p]
    handle, ver = ctypes.c_void_p(), ctypes.c_ulong()
    if wlan.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(handle)) != 0:
        return {"ok": False, "error": "WlanOpenHandle failed"}
    try:
        pl = ctypes.POINTER(_IF_LIST)()
        if wlan.WlanEnumInterfaces(handle, None, ctypes.byref(pl)) != 0 or pl.contents.dwNumberOfItems < 1:
            return {"ok": False, "error": "no wireless interface"}
        guid = _GUID.from_buffer_copy(pl.contents.InterfaceInfo[0].InterfaceGuid)
        wlan.WlanFreeMemory(pl)
        bl = _BSSID_LIST()
        bl.Header.Type, bl.Header.Revision, bl.Header.Size = 0x80, 1, ctypes.sizeof(_BSSID_LIST)
        bl.uNumOfEntries = bl.uTotalNumOfEntries = 1
        for i, b in enumerate(mac):
            bl.BSSIDs[0][i] = b
        p = _CONN_PARAMS()
        p.wlanConnectionMode, p.strProfile = 0, profile
        p.pDot11Ssid, p.pDesiredBssidList = None, ctypes.pointer(bl)
        p.dot11BssType, p.dwFlags = 1, 0
        rc = wlan.WlanConnect(handle, ctypes.byref(guid), ctypes.byref(p), None)
        if rc != 0:
            return {"ok": False, "error": f"WlanConnect error {rc}"}
        return {"ok": True, "bssid": bssid.lower()}
    finally:
        wlan.WlanCloseHandle(handle, None)
