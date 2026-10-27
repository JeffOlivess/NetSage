"""Digital twin of THIS PC's live connection, and the live recovery actions.

The twin models the path  host policy -> Wi-Fi adapter -> access point ->
gateway -> upstream -> DNS  from what the monitor observes (adapter state,
association, per-AP scorecard, gateway/internet/DNS probes) plus a host
policy audit (outbound block rules). Each candidate action is applied to a
copy of that model; only actions predicted to fix the symptom without
breaking a safety rule are executed for real.
"""
from __future__ import annotations

import copy

from . import winnet as w
from .faults import RULE_PREFIX, RULES

LIVE_SAFETY = [
    "Stay on the same authorised SSID - never join another network",
    "Never select an access point marked BAD",
    "Only remove firewall rules that NetSage itself created",
    "Interrupt this PC's connection for at most 10 seconds",
    "Never terminate user processes or change system DNS settings",
]

ACTIONS = {
    "enable_adapter": ("Re-enable the Wi-Fi adapter", "Turn this PC's disabled wireless adapter back on.", 4),
    "reconnect":      ("Reconnect to the Wi-Fi network", "Re-join the saved Wi-Fi profile.", 5),
    "roam_best":      ("Roam to the best access point", "Pin this PC's radio to the best-scoring AP of the same SSID.", 3),
    "remove_rules":   ("Remove the blocking firewall rule", "Delete the NetSage-created outbound block rule found by the host policy audit.", 0),
    "flush_dns":      ("Flush the DNS cache", "Clear Windows' resolver cache (ipconfig /flushdns).", 0),
    "stop_bulk":      ("Stop the bulk transfer", "Halt the bulk download that is saturating the link (NetSage's traffic generator).", 0),
}
PLAYBOOK = {
    "LINK_DOWN":       ["enable_adapter", "reconnect", "roam_best"],
    "PACKET_LOSS":     ["remove_rules", "roam_best", "reconnect"],
    "UPSTREAM_OUTAGE": ["remove_rules", "roam_best", "reconnect"],
    "DNS_FAILURE":     ["remove_rules", "flush_dns", "reconnect"],
    "CONGESTION":      ["stop_bulk", "roam_best"],
    "WEAK_SIGNAL":     ["roam_best", "reconnect"],
    "DEVICE_OVERLOAD": [],
}


class LiveTwin:
    def __init__(self, monitor, injector):
        self.mon = monitor
        self.inj = injector
        self.state = None
        self.rules = []
        self._audit_age = 99
        self.last_signal = 60.0

    def sync(self, feats):
        self._audit_age += 1
        if self._audit_age >= 4:
            self.rules = w.find_rules([f"{RULE_PREFIX}-{x}" for v in RULES.values() for x in v])
            self._audit_age = 0
        ctx = self.mon.context()
        f = feats
        rules = {"loss": any(r.endswith("Loss") for r in self.rules),
                 "outage": any(r.endswith("Outage") for r in self.rules),
                 "dns": any("DNS" in r for r in self.rules)}
        cur = ctx.get("bssid")
        card = self.mon.scorecard
        vis = {a["bssid"] for a in ctx["visible"]}
        best = card.best(visible=vis or None, exclude={cur} if cur else ())
        cur_row = card.aps.get(cur or "", {})
        radio_loss = 0.0
        if f["link_up"] and not rules["loss"]:
            radio_loss = f["gw_loss"] if self.mon.gw_ever_ok else f["inet_loss"]
        gw_ok = bool(f["link_up"]) and f["gw_loss"] < 90
        inet_ok = bool(f["link_up"]) and f["inet_loss"] < 90
        if f["link_up"] and f["signal"] > 0:
            self.last_signal = f["signal"]
        self.state = {
            "admin": w.is_admin(), "adapter_on": ctx["adapter_on"], "connected": ctx["connected"],
            "profile": ctx.get("profile") or ctx.get("ssid") or (self.inj.status() or {}).get("profile"),
            "ssid": ctx.get("ssid"), "bssid": cur, "signal": f["signal"],
            "current_mark": cur_row.get("mark", "none"),
            "rules": rules, "rule_names": list(self.rules), "bulk": self.inj.bulk_running,
            "radio_loss": radio_loss,
            "upstream_down": bool(f["link_up"]) and not inet_ok and not rules["outage"]
                             and (gw_ok or not self.mon.gw_ever_ok),
            "dns_server_down": inet_ok and f["dns_fail"] >= 50 and not rules["dns"],
            "upstream_slow": inet_ok and f["lat_ratio"] >= 2.5 and not self.inj.bulk_running,
            "last_signal": self.last_signal,
            "best": best, "keepdown": (self.inj.status() or {}).get("kind") == "disconnect",
        }
        return self.state

    # ------------------------------------------------------------- model
    @staticmethod
    def predict(s):
        link = s["adapter_on"] and s["connected"]
        loss = (40.0 if s["rules"]["loss"] else 0.0) + s["radio_loss"]
        inet = link and not s["rules"]["outage"] and not s["upstream_down"]
        dns = inet and not s["rules"]["dns"] and not s["dns_server_down"]
        slow = s["bulk"] or s["upstream_slow"]
        return {"link_up": link, "loss": round(min(100.0, loss), 1) if link else 100.0,
                "inet": inet, "dns": dns, "slow": slow, "signal": s["signal"] if link else 0}

    def _apply(self, aid, s):
        s = copy.deepcopy(s)
        if aid == "enable_adapter":
            s["adapter_on"] = True; s["connected"] = bool(s["profile"]); s["signal"] = s["last_signal"]
        elif aid == "reconnect":
            s["connected"] = bool(s["profile"]) and s["adapter_on"]; s["keepdown"] = False
            s["signal"] = s["last_signal"]
        elif aid == "roam_best":
            b = s["best"]
            s["connected"] = s["adapter_on"]; s["bssid"] = b["bssid"]
            s["signal"] = b.get("signal") or s["signal"]
            s["radio_loss"] = min(b["loss"], s["radio_loss"]) if b.get("loss") is not None \
                else max(0.0, (40 - (b.get("signal") or 0)) * 0.8)
            s["current_mark"] = b.get("mark", "none")
        elif aid == "remove_rules":
            s["rules"] = {"loss": False, "outage": False, "dns": False}
        elif aid == "stop_bulk":
            s["bulk"] = False
        return s

    def _precondition(self, aid, s):
        if aid == "enable_adapter":
            if s["adapter_on"]:
                return False, "The adapter is already enabled"
            if not s["admin"]:
                return False, "Needs Administrator rights"
        if aid == "reconnect" and not s["profile"]:
            return False, "No saved Wi-Fi profile is known"
        if aid == "reconnect" and not s["adapter_on"]:
            return True, ""
        if aid == "roam_best":
            if not s["best"]:
                return False, "No other usable access point of this SSID is visible"
            if not s["profile"]:
                return False, "No saved Wi-Fi profile is known"
        if aid == "remove_rules":
            if not s["rule_names"]:
                return False, "Host policy audit found no blocking rule"
            if not s["admin"]:
                return False, "Needs Administrator rights"
        if aid == "stop_bulk" and not s["bulk"]:
            return False, "No bulk transfer is running on this PC"
        return True, ""

    def evaluate_candidates(self, code, projected=False):
        s = self.state
        before = self.predict(s)
        rows = []
        for aid in PLAYBOOK.get(code, []):
            title, detail, secs = ACTIONS[aid]
            if aid == "roam_best" and s["best"]:
                title = f"Roam to access point {s['best']['bssid']}"
            if aid == "remove_rules" and s["rule_names"]:
                detail += " Found: " + ", ".join(s["rule_names"])
            row = {"id": aid, "title": title, "detail": detail, "disruption": round(secs / 60, 2),
                   "notes": []}
            ok, why = self._precondition(aid, s)
            if not ok:
                row.update(verdict="BLOCKED", violations=[why], fixes=False, score=1e9, predicted=None)
                rows.append(row); continue
            after_s = self._apply(aid, s)
            p = self.predict(after_s)
            fixes = p["link_up"] and p["inet"] and p["dns"] and p["loss"] < 5 and not p["slow"] \
                and (p["signal"] or 0) >= 30
            viol = []
            if aid == "roam_best" and after_s["current_mark"] == "bad":
                viol.append("Target access point is marked BAD")
            if aid in ("reconnect", "roam_best") and s["keepdown"]:
                row["notes"].append("Ends the demo's forced disconnection")
            if aid == "reconnect" and not s["adapter_on"]:
                fixes = False
                viol.append("Adapter is disabled - reconnecting cannot work")
            if aid == "flush_dns":
                row["notes"].append("Only helps with stale cached records")
            if secs > 10:
                viol.append("Interrupts the connection for more than 10 s")
            verdict = "INEFFECTIVE" if not fixes else ("UNSAFE" if viol else "SAFE")
            if not fixes:
                for k, msg in (("inet", "internet still unreachable"), ("dns", "DNS still failing"),
                               ("link_up", "Wi-Fi link still down")):
                    if not p[k]:
                        viol.append(f"Twin predicts: {msg}"); break
                else:
                    if p["loss"] >= 5:
                        viol.append(f"Twin predicts: {p['loss']:.0f}% loss remains")
                    elif p["slow"]:
                        viol.append("Twin predicts: latency stays high (cause is upstream)")
            rows.append({**row, "verdict": verdict, "fixes": fixes, "violations": viol,
                         "score": secs + 15 * len(row["notes"]) + (0 if verdict == "SAFE" else 1e6),
                         "predicted": {"inet_loss": p["loss"], "reach": p["inet"], "dns": p["dns"],
                                       "signal": p["signal"], "inet_latency": None}})
        rows.sort(key=lambda r: r["score"])
        if rows and rows[0]["verdict"] == "SAFE":
            rows[0]["verdict"] = "RECOMMENDED"
        return {"projected": projected, "candidates": rows, "constraints": LIVE_SAFETY,
                "before": {"inet_loss": before["loss"], "reach": before["inet"], "dns": before["dns"],
                           "signal": before["signal"], "inet_latency": None}}


class LiveEnv:
    """Executes live actions for the healer."""

    def __init__(self, twin, log):
        self.twin = twin
        self.log = log

    def evaluate(self, code, projected=False):
        return self.twin.evaluate_candidates(code, projected)

    def execute(self, code, aid):
        s = self.twin.state
        mon, inj = self.twin.mon, self.twin.inj
        name = (mon.iface or {}).get("name") or "Wi-Fi"
        if aid == "enable_adapter":
            r = w.set_interface(name, True)
            if r["ok"] and s["profile"]:
                w.reconnect(s["profile"], s.get("ssid"), name)
            return r
        if aid == "reconnect":
            inj.stop_keepdown()
            return w.reconnect(s["profile"], s.get("ssid"), name)
        if aid == "roam_best":
            b = s["best"]
            inj.stop_keepdown()
            r = w.connect_bssid(s["profile"], b["bssid"])
            mon.gw_ever_ok = False
            return r
        if aid == "remove_rules":
            ok = True
            for n in s["rule_names"]:
                if n.startswith(RULE_PREFIX):
                    ok &= w.firewall(f'delete rule name="{n}"')[0]
            inj._stop.set()
            self.twin._audit_age = 99
            return {"ok": ok}
        if aid == "flush_dns":
            return w.flush_dns()
        if aid == "stop_bulk":
            return {"ok": inj.stop_bulk()}
        return {"ok": False, "error": "unknown action"}

    def rollback(self, aid):
        return {"ok": True, "note": "live actions restore service; nothing to undo"}
