"""Live mode against a mocked Windows network (no real Wi-Fi needed).

The mock replaces netsh/ping/DNS/firewall calls with a small fake network so
the full live loop - probes, deep-learning diagnosis, live digital twin,
self-healing action, verification - can be tested on any OS.
"""
import random
import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netsage import config                                       # noqa: E402
from netsage.live import winnet as w                             # noqa: E402


class FakeNet:
    def __init__(self):
        self.rules = {}          # name -> enabled
        self.connected = True
        self.adapter = True
        self.bssid = "30:cb:c7:90:08:60"

    def install(self):
        w.IS_WINDOWS = True
        w.is_admin = lambda: True
        w.current_interface = self.iface
        w.interfaces = lambda: [self.iface()] if self.adapter else []
        w.scan = lambda ssid=None: [
            {"ssid": "F-VIT", "bssid": "30:cb:c7:90:08:60", "signal": 87, "band": "5 GHz", "channel": "157"},
            {"ssid": "F-VIT", "bssid": "a0:f8:49:85:fc:4e", "signal": 65, "band": "5 GHz", "channel": "60"}]
        w.ping_once = self.ping
        w.dns_query = self.dns
        w.default_gateway = lambda: "192.168.43.1"
        w.dns_servers = lambda alias=None: ["192.168.43.1"]
        w.adapter_enabled = lambda name: self.adapter
        w.set_interface = self.set_iface
        w.disconnect = self.disconnect
        w.reconnect = self.reconnect
        w.connect_bssid = self.connect_bssid
        w.firewall = self.firewall
        w.find_rules = lambda names: [n for n in names if n in self.rules]
        w.flush_dns = lambda: {"ok": True}
        w.host_load = lambda: 12.0
        w.nic_bytes = lambda name: None

    def iface(self):
        if not self.adapter:
            return None
        return {"name": "Wi-Fi", "state": "connected" if self.connected else "disconnected",
                "ssid": "F-VIT" if self.connected else "", "profile": "F-VIT",
                "bssid": self.bssid if self.connected else "", "signal": 85 if self.connected else None,
                "band": "5 GHz", "channel": "157"}

    def _blocked(self, host, port=None):
        # The real rule flips every ~0.6 s while live probes run every 1.5 s, so each
        # probe sees an independent ~40% chance of being dropped.
        if "NetSage-Fault-Loss" in self.rules and random.random() < 0.4:
            return True
        if "NetSage-Fault-Outage" in self.rules and not host.startswith("192.168."):
            return True
        if port == 53 and "NetSage-Fault-DNS-UDP" in self.rules:
            return True
        return False

    def ping(self, host, timeout_ms=1000):
        if not (self.adapter and self.connected) or not host or self._blocked(host):
            return None
        return (4.0 if host.startswith("192.168.") else 45.0) * random.uniform(0.85, 1.2)

    def dns(self, server, name="www.google.com", timeout=1.5):
        if not (self.adapter and self.connected) or self._blocked(server, 53):
            return None
        return 20.0

    def set_iface(self, name, enabled=True):
        self.adapter = enabled
        if not enabled:
            self.connected = False
        return {"ok": True, "output": ""}

    def disconnect(self, name=None):
        self.connected = False
        return {"ok": True, "output": ""}

    def reconnect(self, profile, ssid=None, name=None):
        if self.adapter:
            self.connected = True
        return {"ok": True, "output": ""}

    def connect_bssid(self, profile, bssid):
        if self.adapter:
            self.connected, self.bssid = True, bssid
        return {"ok": True}

    def firewall(self, cmd):
        name = re.search(r'name="([^"]+)"', cmd).group(1)
        if cmd.startswith("add"):
            self.rules[name] = "enable=no" not in cmd
        elif cmd.startswith("delete"):
            self.rules.pop(name, None)
        elif cmd.startswith("set") and name in self.rules:
            self.rules[name] = "enable=yes" in cmd
        return True, ""


class TestLiveLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.net = FakeNet()
        cls.net.install()
        config.LIVE_TICK_S = 0.01
        from netsage.engine import Engine
        cls.e = Engine()
        cls.e.running = False
        time.sleep(1.2)
        cls.e.set_mode("live")
        cls.e.settings(auto=True, predictive=False)

    def run_fault(self, kind, expect_code, expect_action, ticks=45):
        e, net = self.e, self.net
        e.healer.reset()
        for _ in range(12):
            e.tick()
        self.assertEqual(e.diag["code"], "HEALTHY")
        r = e.inject(kind, 300)
        self.assertTrue(r["ok"], r)
        codes = []
        for _ in range(ticks):
            e.tick(); codes.append(e.diag["code"])
            time.sleep(0.15)
        inc = e.healer.snapshot()["incidents"][0]
        e.live["inj"].clear("test")
        net.rules.clear(); net.adapter = True; net.connected = True
        return inc, codes

    def test_packet_loss(self):
        inc, codes = self.run_fault("loss", "PACKET_LOSS", "remove_rules")
        self.assertEqual(inc["code"], "PACKET_LOSS", codes)
        self.assertEqual(inc["chosen"]["id"], "remove_rules")
        self.assertEqual(inc["outcome"], "recovered")

    def test_outage(self):
        inc, codes = self.run_fault("outage", "UPSTREAM_OUTAGE", "remove_rules")
        self.assertEqual(inc["code"], "UPSTREAM_OUTAGE", codes)
        self.assertEqual(inc["chosen"]["id"], "remove_rules")
        self.assertEqual(inc["outcome"], "recovered")

    def test_dns(self):
        inc, codes = self.run_fault("dns", "DNS_FAILURE", "remove_rules")
        self.assertEqual(inc["code"], "DNS_FAILURE", codes)
        self.assertEqual(inc["chosen"]["id"], "remove_rules")
        self.assertEqual(inc["outcome"], "recovered")

    def test_adapter(self):
        inc, codes = self.run_fault("adapter", "LINK_DOWN", "enable_adapter")
        self.assertEqual(inc["code"], "LINK_DOWN", codes)
        self.assertEqual(inc["chosen"]["id"], "enable_adapter")
        self.assertEqual(inc["outcome"], "recovered")
        reconnect = next(c for c in inc["plan"]["candidates"] if c["id"] == "reconnect")
        self.assertNotIn(reconnect["verdict"], ("SAFE", "RECOMMENDED"))

    def test_disconnect(self):
        inc, codes = self.run_fault("disconnect", "LINK_DOWN", "reconnect")
        self.assertEqual(inc["code"], "LINK_DOWN", codes)
        self.assertIn(inc["chosen"]["id"], ("reconnect", "roam_best"))
        self.assertEqual(inc["outcome"], "recovered")


if __name__ == "__main__":
    unittest.main()
