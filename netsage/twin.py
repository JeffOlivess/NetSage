"""Digital twin of the enterprise network.

The twin is a continuously synchronised model of the production network. It is
used for two things:

  1. Safe self-healing -- every candidate recovery action is first executed on a
     copy of the twin; the predicted outcome is checked against safety
     constraints (reachability, DNS, SLA, link and AP capacity, device CPU,
     redundancy, disruption budget). Only a SAFE action may touch production.
  2. What-if projection -- when the predictor warns that a fault is coming, the
     twin extrapolates the current trends to see the network a few seconds
     ahead and tests pre-emptive actions against that future state.
"""
from __future__ import annotations

from collections import deque

import numpy as np

from . import config
from . import playbooks
from .netmodel import baseline_state, clone, evaluate


class DigitalTwin:
    def __init__(self, state):
        self.state = clone(state)
        self.baseline = evaluate(self.state)
        # healthy design baseline of the monitored path (reference for "is it fixed?")
        self.base_latency = evaluate(baseline_state())["probe"]["inet_latency"]
        self.history = deque(maxlen=config.PRED_WINDOW)
        self.last_eval = self.baseline
        self.fidelity = None
        self.syncs = 0

    # ------------------------------------------------------------ sync
    def sync(self, observed_state, measured_probe=None):
        """Mirror the production network (streaming telemetry / SNMP / config)."""
        self.state = clone(observed_state)
        self.history.append(self._drivers(self.state))
        self.last_eval = evaluate(self.state)
        self.syncs += 1
        if measured_probe and self.last_eval["probe"]["inet_latency"] and measured_probe.get("inet_latency"):
            pred = self.last_eval["probe"]["inet_latency"]
            meas = measured_probe["inet_latency"]
            if meas < 999:
                err = abs(pred - meas) / max(meas, 1)
                f = max(0.0, 1 - err) * 100
                self.fidelity = f if self.fidelity is None else 0.8 * self.fidelity + 0.2 * f
        return self.last_eval

    @staticmethod
    def _drivers(st):
        d = {"backup": st["demand"]["backup"], "interf": st["ext_interference"]["level"]}
        for n, nd in st["nodes"].items():
            d[f"storm:{n}"] = nd.get("cpu_storm", 0.0)
        for k, L in st["links"].items():
            d[f"err:{k}"] = L["loss_err"]
        return d

    def project(self, horizon=config.PRED_HORIZON):
        """Twin state `horizon` ticks ahead, by extrapolating current trends."""
        st = clone(self.state)
        if len(self.history) < 4:
            return st
        keys = self.history[-1].keys()
        xs = np.arange(len(self.history)) - (len(self.history) - 1) / 2
        for k in keys:
            ys = np.array([h.get(k, 0.0) for h in self.history])
            slope = float((xs * (ys - ys.mean())).sum() / (xs ** 2).sum())
            if slope <= 1e-6:
                continue
            v = float(ys[-1] + slope * horizon)
            if k == "backup":
                st["demand"]["backup"] = min(900.0, v)
            elif k == "interf":
                st["ext_interference"]["level"] = min(1.0, v)
            elif k.startswith("storm:"):
                st["nodes"][k[6:]]["cpu_storm"] = min(85.0, v)
            elif k.startswith("err:"):
                st["links"][k[4:]]["loss_err"] = min(60.0, v)
        return st

    # ------------------------------------------------------------ what-if
    def evaluate_candidates(self, code, projected=False):
        base = self.project() if projected else clone(self.state)
        before = evaluate(base)
        results = []
        for act in playbooks.candidates(code, base):
            ok, why = act.precondition(base)
            row = {"id": act.id, "title": act.title, "detail": act.detail,
                   "disruption": act.disruption, "notes": list(act.notes)}
            if not ok:
                row.update(verdict="BLOCKED", fixes=False, violations=[why], score=1e9,
                           predicted=None)
                results.append(row)
                continue
            sim = clone(base)
            act.apply(sim)
            after = evaluate(sim)
            fixes = self._fixes(after)
            viol = self._violations(after, act, before)
            if not fixes:
                verdict = "INEFFECTIVE"
            elif viol:
                verdict = "UNSAFE"
            else:
                verdict = "SAFE"
            pr = after["probe"]
            worst_util = max((l["util"] for l in after["links"].values() if l["up"]), default=0)
            score = act.disruption + 0.3 * worst_util + 15 * len(act.notes)
            if verdict != "SAFE":
                score += 1e6
            row.update(verdict=verdict, fixes=fixes, violations=viol, score=round(score, 1),
                       predicted={"inet_latency": _r(pr["inet_latency"]), "inet_loss": _r(pr["inet_loss"]),
                                  "dns": pr["dns_ext"], "reach": pr["reach_inet"],
                                  "signal": _r(pr["signal"]), "worst_util": _r(worst_util),
                                  "max_cpu": _r(max(n["cpu"] for n in after["nodes"].values()))})
            results.append(row)
        results.sort(key=lambda r: r["score"])
        if results and results[0]["verdict"] == "SAFE":
            results[0]["verdict"] = "RECOMMENDED"
        return {"projected": projected, "candidates": results,
                "before": _summary(before), "constraints": constraint_list()}

    def _fixes(self, ev):
        p = ev["probe"]
        if not (p["link_up"] and p["reach_inet"] and p["dns_ext"]):
            return False
        if p["inet_loss"] >= 5:
            return False
        lat_limit = max(4 * self.base_latency, 150)
        return p["inet_latency"] is not None and p["inet_latency"] < lat_limit and p["signal"] >= 35

    def _violations(self, ev, act, before):
        v = []
        for key, g in ev["groups"].items():
            if g["users"] <= 0:
                continue
            if not g["link_up"]:
                v.append(f"{key}: {g['users']} users left without Wi-Fi"); continue
            if not g["reach_inet"]:
                v.append(f"{key}: internet unreachable")
            if not g["reach_app"]:
                v.append(f"{key}: ERP application unreachable")
            if not g["dns_int"]:
                v.append(f"{key}: internal names (erp.corp) cannot be resolved")
            elif not g["dns_ext"]:
                v.append(f"{key}: external DNS fails")
            if g["reach_inet"] and g["inet_loss"] > config.SLA_LOSS:
                v.append(f"{key}: {g['inet_loss']:.1f}% loss breaks the {config.SLA_LOSS:.0f}% SLA")
            if g["reach_inet"] and g["inet_latency"] and g["inet_latency"] > config.SLA_LATENCY:
                v.append(f"{key}: {g['inet_latency']:.0f} ms latency breaks the "
                         f"{config.SLA_LATENCY:.0f} ms SLA")
        for k, L in ev["links"].items():
            if L["up"] and L["util"] > config.MAX_LINK_UTIL:
                v.append(f"Link {k.replace('|', '-')} at {L['util']:.0f}% (limit {config.MAX_LINK_UTIL:.0f}%)")
        for n, nd in ev["nodes"].items():
            if nd["up"] and nd["cpu"] > config.MAX_CPU:
                v.append(f"{n} CPU at {nd['cpu']:.0f}% (limit {config.MAX_CPU:.0f}%)")
        for ap, a in ev["aps"].items():
            if a["up"] and a["load"] > 100:
                v.append(f"{ap} over capacity: {a['users']} clients ({a['load']:.0f}% of airtime budget)")
            if a["up"] and a["interference"] >= 0.3 and a["users"] > 0:
                v.append(f"{ap} suffers co-channel interference ({a['interference']:.2f})")
        for acc, ok in ev["redundant"].items():
            if not ok and before["redundant"].get(acc, True):
                v.append(f"{acc} loses its redundant uplink (single point of failure)")
        if act.disruption > config.DISRUPTION_BUDGET:
            v.append(f"Disruption {act.disruption:.0f} user-min exceeds the "
                     f"{config.DISRUPTION_BUDGET:.0f} user-min budget")
        seen, out = set(), []
        for x in v:
            if x not in seen:
                seen.add(x); out.append(x)
        return out


def _r(x):
    return None if x is None else round(float(x), 1)


def _summary(ev):
    p = ev["probe"]
    return {"inet_latency": _r(p["inet_latency"]), "inet_loss": _r(p["inet_loss"]),
            "dns": p["dns_ext"], "reach": p["reach_inet"], "signal": _r(p["signal"])}


def constraint_list():
    return [
        "Every user group keeps internet, ERP and DNS (internal + external) reachability",
        f"Packet loss <= {config.SLA_LOSS:.0f}% and latency <= {config.SLA_LATENCY:.0f} ms for all groups",
        f"No link above {config.MAX_LINK_UTIL:.0f}% utilisation",
        f"No device CPU above {config.MAX_CPU:.0f}%",
        "No access point over its client capacity or on a clashing channel",
        "No access switch loses its redundant uplink",
        f"Service disruption within {config.DISRUPTION_BUDGET:.0f} user-minutes",
    ]
