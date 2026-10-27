"""Closed-loop self-healing:  detect -> diagnose -> plan on the digital twin
-> act -> verify -> (rollback / next candidate / escalate).

Works with any environment object that offers:
    evaluate(code, projected) -> twin evaluation dict (candidates ranked)
    execute(code, action_id)  -> {"ok": bool, ...}
    rollback(action_id)       -> {"ok": bool}
"""
from __future__ import annotations

import itertools
import time
from collections import deque

from . import config
from .features import CLASS_INFO

_ids = itertools.count(1)


class Healer:
    def __init__(self, env, log, verify_ticks=None):
        self.env = env
        self.log = log
        self.verify_ticks = verify_ticks or config.VERIFY_TICKS
        self.auto = True
        self.predictive = True
        self.reset()

    def reset(self):
        self.phase = "MONITORING"
        self.incident = None
        self.incidents = deque(maxlen=25)
        self.streak_code, self.streak = None, 0
        self.risk_streak = 0
        self.cooldown = 0
        self._queue = []
        self._verify = 0
        self._healthy = 0

    # ------------------------------------------------------------------ main
    def tick(self, diag, pred):
        code = diag["code"]
        if self.cooldown:
            self.cooldown -= 1
        if self.phase == "VERIFYING":
            return self._verify_tick(code, pred)
        if self.phase in ("AWAITING_APPROVAL", "ESCALATED"):
            if code == "HEALTHY" and pred.get("risk", 0) < config.RISK_ALERT:
                self._healthy += 1
                if self._healthy >= 3:
                    self._close("self-resolved" if self.phase == "AWAITING_APPROVAL" else "resolved")
            else:
                self._healthy = 0
            return

        # MONITORING
        if code != "HEALTHY":
            self.streak = self.streak + 1 if code == self.streak_code else 1
            self.streak_code = code
            self.risk_streak = 0
            if self.streak >= config.CONFIRM_TICKS:
                self._open("reactive", code, diag)
            return
        self.streak, self.streak_code = 0, None
        if self.predictive and pred.get("forecast") and pred["risk"] >= config.RISK_ALERT \
                and not self.cooldown:
            self.risk_streak += 1
            if self.risk_streak >= 3:
                self._open("preventive", pred["forecast"], diag, pred)
        else:
            self.risk_streak = 0

    # --------------------------------------------------------------- incidents
    def _open(self, kind, code, diag, pred=None):
        now = time.time()
        inc = {"id": next(_ids), "kind": kind, "code": code, "title": CLASS_INFO[code]["title"],
               "severity": CLASS_INFO[code]["severity"], "opened": now, "closed": None,
               "confidence": diag["confidence"] if kind == "reactive" else pred["risk"],
               "evidence": diag["evidence"] if kind == "reactive" else
               [f"Predicted {CLASS_INFO[code]['title'].lower()} with {pred['risk']*100:.0f}% risk "
                f"within ~{config.PRED_HORIZON} s"] + [f"Driver: {d['label']}" for d in pred["drivers"]],
               "plan": None, "playbook": None, "tried": [], "chosen": None, "outcome": "open",
               "steps": []}
        self.incident = inc
        self.incidents.appendleft(inc)
        self._step(f"{'Early warning' if kind == 'preventive' else 'Fault confirmed'}: "
                   f"{inc['title']} ({inc['confidence']*100:.0f}%)")
        self.log("detect" if kind == "reactive" else "predict",
                 f"{'Predicted' if kind == 'preventive' else 'Detected'} {inc['title'].lower()}"
                 f" - confidence {inc['confidence']*100:.0f}%")
        if kind == "preventive":
            diag = {"probabilities": pred.get("forecast_probs", {})}
        self._plan(code, diag, projected=(kind == "preventive"))

    def _plan(self, code, diag, projected):
        inc = self.incident
        order = [code] + [c for c, _ in sorted(diag["probabilities"].items(), key=lambda t: -t[1])
                          if c not in (code, "HEALTHY")]
        chosen_eval = None
        for i, c in enumerate(order):
            ev = self.env.evaluate(c, projected)
            if not ev["candidates"]:
                continue
            if chosen_eval is None:
                chosen_eval = (c, ev)
            if any(r["verdict"] in ("RECOMMENDED", "SAFE") for r in ev["candidates"]):
                chosen_eval = (c, ev)
                if c != code:
                    self._step(f"No safe fix in the {CLASS_INFO[code]['title'].lower()} playbook; "
                               f"twin found one in the {CLASS_INFO[c]['title'].lower()} playbook")
                break
        if chosen_eval is None:
            if projected:
                self._close("warned")
                return
            self._escalate("No playbook applies to this fault")
            return
        c, ev = chosen_eval
        inc["plan"], inc["playbook"] = ev, c
        n_safe = sum(r["verdict"] in ("RECOMMENDED", "SAFE") for r in ev["candidates"])
        n_bad = len(ev["candidates"]) - n_safe
        self._step(f"Digital twin simulated {len(ev['candidates'])} candidate actions"
                   f"{' on the projected network' if projected else ''}: "
                   f"{n_safe} safe, {n_bad} rejected")
        self.log("twin", f"Twin: {n_safe} safe / {n_bad} rejected for {CLASS_INFO[c]['title'].lower()}")
        self._queue = [r for r in ev["candidates"] if r["verdict"] in ("RECOMMENDED", "SAFE")]
        if not self._queue:
            if projected:   # an early warning without a safe pre-emptive action is just logged
                self._close("warned")
                return
            self._escalate("Every candidate was ineffective or unsafe in the twin")
            return
        if self.auto:
            self._execute(self._queue.pop(0))
        else:
            self.phase = "AWAITING_APPROVAL"
            self._step(f"Awaiting operator approval for: {self._queue[0]['title']}")
            self.log("plan", f"Recommended (awaiting approval): {self._queue[0]['title']}")

    def approve(self, action_id=None):
        if self.phase != "AWAITING_APPROVAL" or not self.incident:
            return {"ok": False, "error": "nothing awaiting approval"}
        cands = self.incident["plan"]["candidates"]
        pick = next((r for r in cands if r["id"] == action_id), None) if action_id else self._queue[0]
        if pick is None:
            return {"ok": False, "error": "unknown action"}
        if pick["verdict"] not in ("RECOMMENDED", "SAFE"):
            return {"ok": False, "error": "the digital twin marked this action unsafe"}
        self._queue = [r for r in self._queue if r["id"] != pick["id"]]
        self._execute(pick)
        return {"ok": True}

    def _execute(self, cand):
        inc = self.incident
        res = self.env.execute(inc["playbook"], cand["id"])
        inc["tried"].append(cand["id"])
        if not res.get("ok"):
            self._step(f"Action failed: {cand['title']} ({res.get('error', 'error')})")
            self.log("error", f"Action failed: {cand['title']}")
            if self._queue:
                return self._execute(self._queue.pop(0))
            return self._escalate("All safe actions failed to execute")
        inc["chosen"] = cand
        inc["executed"] = time.time()
        self.phase = "VERIFYING"
        self._verify = 0
        self._healthy = 0
        self._step(f"Executed: {cand['title']}")
        self.log("heal", f"Self-healing action: {cand['title']}")

    def _verify_tick(self, code, pred):
        inc = self.incident
        self._verify += 1
        ok = code == "HEALTHY" and (inc["kind"] == "reactive" or pred.get("risk", 0) < config.RISK_ALERT)
        self._healthy = self._healthy + 1 if ok else 0
        if self._healthy >= 3:
            self._close("prevented" if inc["kind"] == "preventive" else "recovered")
            return
        if self._verify >= self.verify_ticks + (6 if inc["kind"] == "preventive" else 0):
            self.env.rollback(inc["chosen"]["id"])
            self._step(f"Verification failed - rolled back: {inc['chosen']['title']}")
            self.log("rollback", f"Rolled back: {inc['chosen']['title']}")
            if self._queue:
                self._execute(self._queue.pop(0))
            else:
                self._escalate("Safe actions did not restore service")

    def _escalate(self, why):
        self.phase = "ESCALATED"
        self._healthy = 0
        if self.incident:
            self.incident["outcome"] = "escalated"
            self._step(f"Escalated to the network team: {why}")
        self.log("escalate", f"Escalated: {why}")

    def _close(self, outcome):
        inc = self.incident
        if inc:
            inc["closed"] = time.time()
            inc["outcome"] = outcome
            inc["mttr_s"] = round(inc["closed"] - inc["opened"], 1)
            label = {"recovered": "Service restored", "prevented": "Fault prevented",
                     "self-resolved": "Fault cleared by itself", "resolved": "Fault resolved",
                     "warned": "Early warning logged (no safe pre-emptive action)"}[outcome]
            self._step(f"{label} - {inc['mttr_s']:.0f} s after detection")
            self.log("recover", f"{label}: {inc['title']} in {inc['mttr_s']:.0f} s")
        self.incident = None
        self.phase = "MONITORING"
        self.streak, self.streak_code, self.risk_streak = 0, None, 0
        self.cooldown = 30 if outcome == "warned" else 10

    def _step(self, text):
        if self.incident is not None:
            self.incident["steps"].append({"t": time.time(), "text": text})

    def snapshot(self):
        return {"phase": self.phase, "auto": self.auto, "predictive": self.predictive,
                "incident": self.incident, "incidents": list(self.incidents)[:12],
                "queue": [r["id"] for r in self._queue]}

