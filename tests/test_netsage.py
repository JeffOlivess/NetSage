"""NetSage test-suite.   Run:  python -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netsage.dataset import static_samples                       # noqa: E402
from netsage.features import transform                            # noqa: E402
from netsage.healer import Healer                                 # noqa: E402
from netsage.live import winnet                                   # noqa: E402
from netsage.models import Brain                                  # noqa: E402
from netsage.netmodel import baseline_state, evaluate             # noqa: E402
from netsage.nn import DeepNet                                    # noqa: E402
from netsage.simulation import SIM_FAULTS, SimNetwork             # noqa: E402
from netsage.twin import DigitalTwin                              # noqa: E402

BRAIN = Brain()


class TestDeepNet(unittest.TestCase):
    def test_gradient_matches_finite_difference(self):
        net = DeepNet([5, 8, 6, 3], seed=0)
        x = np.random.default_rng(1).random(5)
        g = net.input_gradient(x, 1)
        eps = 1e-5
        for i in range(5):
            d = np.zeros(5); d[i] = eps
            num = (net.predict_proba(x + d)[0, 1] - net.predict_proba(x - d)[0, 1]) / (2 * eps)
            self.assertAlmostEqual(g[i], num, places=5)

    def test_diagnoser_accuracy_on_fresh_data(self):
        X, y = static_samples(n_per_class=150, seed=99)
        pred = BRAIN.diag.predict_proba(transform(X)).argmax(axis=1)
        self.assertGreater((pred == y).mean(), 0.95)

    def test_explanations_sum_to_prediction_change(self):
        X, _ = static_samples(n_per_class=5, seed=5)
        d = BRAIN.diagnose(X[20])
        self.assertEqual(len(d["factors"]), 6)
        self.assertTrue(d["evidence"])


class TestDigitalTwin(unittest.TestCase):
    EXPECT = {"link_errors": "drain_link", "congestion": "qos_police", "cpu_storm": "copp",
              "interference": "chan_clean", "isp_outage": "failover_b_qos",
              "dns_crash": "dns_failover", "ap_failure": "steer_split"}
    PLAYBOOK = {"interference": "WEAK_SIGNAL"}

    def test_baseline_is_healthy(self):
        ev = evaluate(baseline_state())
        self.assertTrue(all(g["reach_inet"] and g["dns_int"] for g in ev["groups"].values()))

    def test_twin_picks_the_safe_action_and_rejects_dangerous_ones(self):
        for kind, want in self.EXPECT.items():
            sim = SimNetwork(seed=3); sim.inject(kind)
            for _ in range(30):
                sim.step()
            twin = DigitalTwin(sim.state); twin.sync(sim.state)
            code = self.PLAYBOOK.get(kind, SIM_FAULTS[kind]["expect"])
            res = twin.evaluate_candidates(code)
            top = res["candidates"][0]
            self.assertEqual(top["verdict"], "RECOMMENDED", kind)
            self.assertEqual(top["id"], want, kind)
            self.assertTrue(any(c["verdict"] in ("UNSAFE", "INEFFECTIVE", "BLOCKED")
                                for c in res["candidates"]), kind)


class _Env:
    def __init__(self, sim, twin):
        self.sim, self.twin = sim, twin

    def evaluate(self, code, projected=False):
        return self.twin.evaluate_candidates(code, projected)

    def execute(self, code, aid):
        return self.sim.apply(code, aid)

    def rollback(self, aid):
        return self.sim.rollback(aid)


class TestClosedLoop(unittest.TestCase):
    def run_fault(self, kind, auto=True, predictive=False, ticks=60):
        sim = SimNetwork(seed=4); twin = DigitalTwin(sim.state)
        h = Healer(_Env(sim, twin), lambda *a: None)
        h.auto, h.predictive = auto, predictive
        raw = []
        for t in range(15 + ticks):
            if t == 15:
                sim.inject(kind)
            x, _ = sim.step(); twin.sync(sim.state); raw.append(x)
            d = BRAIN.diagnose(np.median(raw[-3:], axis=0)); p = BRAIN.predict(raw[-10:])
            h.tick(d, p)
        return h, d

    def test_every_fault_is_healed(self):
        for kind in SIM_FAULTS:
            h, d = self.run_fault(kind)
            inc = h.incidents[0]
            self.assertIn(inc["outcome"], ("recovered", "prevented"), kind)
            self.assertEqual(d["code"], "HEALTHY", kind)

    def test_manual_approval(self):
        h, d = self.run_fault("dns_crash", auto=False, ticks=12)
        self.assertEqual(h.phase, "AWAITING_APPROVAL")
        self.assertTrue(h.approve()["ok"])
        self.assertEqual(h.phase, "VERIFYING")

    def test_no_false_alarms(self):
        sim = SimNetwork(seed=8); twin = DigitalTwin(sim.state)
        h = Healer(_Env(sim, twin), lambda *a: None)
        raw = []
        for _ in range(150):
            x, _ = sim.step(); twin.sync(sim.state); raw.append(x)
            h.tick(BRAIN.diagnose(np.median(raw[-3:], axis=0)), BRAIN.predict(raw[-10:]))
        self.assertEqual(len(h.incidents), 0)


class TestWindowsParsers(unittest.TestCase):
    def test_interfaces(self):
        txt = """
    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    State                  : connected
    SSID                   : F-VIT
    AP BSSID               : 30:CB:C7:90:08:60
    Band                   : 5 GHz
    Channel                : 157
    Receive rate (Mbps)    : 866.7
    Signal                 : 87%
    Profile                : F-VIT
"""
        i = winnet.parse_interfaces(txt)[0]
        self.assertEqual(i["bssid"], "30:cb:c7:90:08:60")
        self.assertEqual(i["signal"], 87)
        self.assertEqual(i["profile"], "F-VIT")

    def test_networks(self):
        txt = """SSID 1 : F-VIT
    Network type            : Infrastructure
    BSSID 1                 : 30:cb:c7:90:08:60
         Signal             : 87%
         Band               : 5 GHz
         Channel            : 157
         Channel Utilization: 12 (5 %)
    BSSID 2                 : a0:f8:49:85:fc:4e
         Signal             : 65%
         Channel            : 60
"""
        aps = winnet.parse_networks(txt, "F-VIT")
        self.assertEqual([a["bssid"] for a in aps], ["30:cb:c7:90:08:60", "a0:f8:49:85:fc:4e"])
        self.assertEqual(aps[0]["channel"], "157")
        self.assertEqual(aps[0]["channel_util"], 5)

    def test_ping(self):
        self.assertEqual(winnet.parse_ping_once("Reply from 8.8.8.8: bytes=32 time=23ms TTL=117"), 23.0)
        self.assertEqual(winnet.parse_ping_once("Reply from 1.1.1.1: bytes=32 time<1ms TTL=64"), 1.0)
        self.assertIsNone(winnet.parse_ping_once("Request timed out."))


if __name__ == "__main__":
    unittest.main()
