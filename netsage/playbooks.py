"""Recovery playbooks for the simulated enterprise network.

For each diagnosed fault class NetSage proposes several candidate actions --
including tempting but dangerous ones. None of them is executed blindly: the
digital twin first replays each candidate on a copy of the live network and
only an action that fixes the fault without breaking anything else is used.
"""
from __future__ import annotations

from .netmodel import ap_interference, lid, node_up


class Action:
    def __init__(self, aid, title, detail, apply, users=0, seconds=0.0,
                 precondition=None, notes=None, config_change=True):
        self.id, self.title, self.detail = aid, title, detail
        self._apply = apply
        self.users, self.seconds = users, seconds
        self.precondition = precondition or (lambda st: (True, ""))
        self.notes = notes or []
        self.config_change = config_change

    def apply(self, st):
        self._apply(st)

    @property
    def disruption(self):
        return round(self.users * self.seconds / 60.0, 1)


def _total_users(st):
    return sum(g["users"] for g in st["groups"].values())


def _users_on_switch(st, sw):
    aps = [k for k, L in st["links"].items() if sw in (L["a"], L["b"])
           and st["nodes"][L["b"] if L["a"] == sw else L["a"]]["kind"] == "ap"]
    aps = [st["links"][k]["a"] if st["links"][k]["b"] == sw else st["links"][k]["b"] for k in aps]
    return round(sum(g["users"] * f for g in st["groups"].values()
                     for a, f in g["assoc"].items() if a in aps))


def _find_err_link(st):
    bad = [k for k, L in st["links"].items() if L["loss_err"] > 0.5]
    return max(bad, key=lambda k: st["links"][k]["loss_err"]) if bad else None


def _hot_node(st):
    hot = [n for n, d in st["nodes"].items() if d.get("cpu_storm", 0) > 5]
    return max(hot, key=lambda n: st["nodes"][n]["cpu_storm"]) if hot else None


# ------------------------------------------------------------------ playbooks
def candidates(code, st):
    total = _total_users(st)
    out = []

    if code == "PACKET_LOSS":
        k = _find_err_link(st) or lid("CORE-1", "ACC-1")
        a, b = st["links"][k]["a"], st["links"][k]["b"]
        acc = a if a.startswith("ACC") else b
        users = _users_on_switch(st, acc) if acc.startswith("ACC") else total

        def drain(s, k=k):
            s["links"][k]["cost"] = 50
        def shut(s, k=k):
            s["links"][k]["admin_up"] = False
        def bounce(s):
            pass   # a shut/no-shut does not clear a physical (CRC) error source
        out += [
            Action("drain_link", f"Drain traffic off {a}-{b}",
                   f"Raise the routing cost of the faulty link {a}-{b} so traffic takes the "
                   "redundant path; the link stays up as a standby.", drain, 0, 0),
            Action("shut_link", f"Shut down link {a}-{b}",
                   "Administratively disable the faulty link.", shut, users, 2),
            Action("bounce_link", f"Bounce interface {a}-{b} (shut / no shut)",
                   "Reset the interface hoping the errors clear.", bounce, users, 6,
                   notes=["CRC/physical errors persist after an interface reset"]),
        ]

    elif code == "CONGESTION":
        def qos(s):
            s["policy"]["qos_bulk_cap"] = 250
        def share(s):
            s["policy"]["isp_mode"] = "share"
        def move_b(s):
            s["policy"]["isp_mode"] = "B"
        out += [
            Action("qos_police", "Apply QoS: police bulk / backup traffic to 250 Mb/s",
                   "Mark business traffic as priority and rate-limit the bulk class "
                   "(backups, updates) at the edge.", qos, 0, 0),
            Action("isp_share", "Load-share internet traffic across ISP-A and ISP-B",
                   "Split outbound traffic 2:1 across both providers.", share, 0, 0,
                   notes=["Uses the metered backup link"]),
            Action("move_isp_b", "Move all internet traffic to ISP-B",
                   "Switch the default route to the backup provider.", move_b, total, 3),
        ]

    elif code == "UPSTREAM_OUTAGE":
        def to_b(s):
            s["policy"]["isp_mode"] = "B"
        def to_b_qos(s):
            s["policy"]["isp_mode"] = "B"; s["policy"]["qos_bulk_cap"] = 100
        def reboot_edge(s):
            pass   # the provider link stays down after a local reboot
        out += [
            Action("failover_b", "Fail over default route to ISP-B",
                   "Point the default route at the backup provider.", to_b, total, 2),
            Action("failover_b_qos", "Fail over to ISP-B and police bulk traffic to 100 Mb/s",
                   "Fail over, and protect business traffic on the smaller backup link "
                   "with a QoS policy.", to_b_qos, total, 2),
            Action("reboot_edge", "Reboot the edge router",
                   "Power-cycle the edge router.", reboot_edge, total, 180,
                   notes=["Fault is in the provider network, not the edge router"]),
        ]

    elif code == "DNS_FAILURE":
        cur = st["policy"]["dns_server"]
        alt = "DNS-2" if cur != "DNS-2" else "DNS-1"

        def switch(s, alt=alt):
            s["policy"]["dns_server"] = alt
        def restart(s, cur=cur):
            if cur in s["nodes"] and node_up(s, cur):
                s["nodes"][cur]["service_up"] = True
        def public(s):
            s["policy"]["dns_server"] = "PUBLIC"
        out += [
            Action("dns_failover", f"Fail over clients to {alt}",
                   f"Update the DHCP DNS option so clients resolve via {alt}.", switch, 0, 0),
            Action("dns_restart", f"Restart the DNS service on {cur}",
                   "Restart the resolver process; clients wait for it to come back.",
                   restart, total, 45, notes=["Root cause of the crash is unknown"]),
            Action("dns_public", "Point clients at a public resolver (8.8.8.8)",
                   "Bypass the internal DNS servers.", public, 0, 0),
        ]

    elif code == "LINK_DOWN":
        dead = [a for a, d in st["nodes"].items() if d["kind"] == "ap" and not node_up(st, a)]
        ap = dead[0] if dead else "AP-1"
        others = [a for a, d in st["nodes"].items() if d["kind"] == "ap" and a != ap]
        same_floor = [a for a in others if st["nodes"][a].get("floor") == st["nodes"][ap].get("floor")]
        near = same_floor[0] if same_floor else others[0]
        far = [a for a in others if a != near][0]

        def move_all(s, ap=ap, near=near):
            for g in s["groups"].values():
                if ap in g["assoc"]:
                    f = g["assoc"].pop(ap); g["assoc"][near] = g["assoc"].get(near, 0) + f
        def split(s, ap=ap, near=near, far=far):
            for g in s["groups"].values():
                if ap in g["assoc"]:
                    f = g["assoc"].pop(ap)
                    g["assoc"][near] = g["assoc"].get(near, 0) + f / 2
                    g["assoc"][far] = g["assoc"].get(far, 0) + f / 2
        def reboot(s, ap=ap):
            pass
        out += [
            Action("steer_near", f"Steer all {ap} clients to {near}",
                   f"Send 802.11v BSS-transition requests moving every client to {near}.",
                   move_all, 0, 0),
            Action("steer_split", f"Split {ap} clients across {near} and {far}",
                   f"Balance the orphaned clients between {near} and {far} using "
                   "802.11v BSS-transition and band steering.", split, 0, 0),
            Action("reboot_ap", f"Remote-reboot {ap}",
                   "Power-cycle the access point via PoE.", reboot, 0, 120,
                   precondition=lambda s, ap=ap: (node_up(s, ap),
                                                  f"{ap} is unreachable on the management network"
                                                  if not node_up(s, ap) else "")),
        ]

    elif code == "DEVICE_OVERLOAD":
        hot = _hot_node(st) or "CORE-1"

        def copp(s):
            s["policy"]["copp"] = True
        def reboot(s, hot=hot):
            s["nodes"][hot]["cpu_storm"] = 0.0
        def shut(s, hot=hot):
            s["nodes"][hot]["admin_up"] = False
        out += [
            Action("copp", f"Apply control-plane policing + storm control on {hot}",
                   "Rate-limit the flood of control-plane / broadcast packets hitting "
                   "the CPU.", copp, 0, 0),
            Action("reboot_dev", f"Reboot {hot}",
                   "Restart the device; traffic reconverges on the redundant core.", reboot,
                   total, 8, notes=["The traffic storm will build up again after the reboot"]),
            Action("isolate_dev", f"Isolate {hot} (shut it down)",
                   "Take the device out of service.", shut, total, 4),
        ]

    elif code == "WEAK_SIGNAL":
        ap = "AP-1"
        worst, wv = None, -1
        for a, d in st["nodes"].items():
            if d["kind"] == "ap" and node_up(st, a):
                v = ap_interference(st, a)
                if v > wv:
                    worst, wv = a, v
        ap = worst or ap
        used = {d["channel"] for a, d in st["nodes"].items() if d["kind"] == "ap" and a != ap}
        clash = sorted(used)[-1]
        clean = next(c for c in (157, 161, 165, 40, 48) if c not in used)
        users = round(sum(g["users"] * f for g in st["groups"].values()
                          for a, f in g["assoc"].items() if a == ap))

        def chan(s, c, ap=ap):
            s["nodes"][ap]["channel"] = c
        def power(s, ap=ap):
            s["nodes"][ap]["tx_boost"] = 2
        out += [
            Action("chan_clash", f"Move {ap} to channel {clash}",
                   f"Leave the interfered channel for {clash}.",
                   lambda s, c=clash: chan(s, c), users, 3),
            Action("chan_clean", f"Move {ap} to channel {clean}",
                   f"Move to channel {clean}, which no neighbouring AP or interferer uses.",
                   lambda s, c=clean: chan(s, c), users, 3),
            Action("tx_power", f"Raise {ap} transmit power to maximum",
                   "Boost the AP's transmit power to overpower the interference.", power, 0, 0),
        ]
    return out
