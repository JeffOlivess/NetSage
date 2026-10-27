"""Command-line access-point tool (Windows).

  python -m netsage.live.wifi_cli scan  --ssid F-VIT
  python -m netsage.live.wifi_cli mark  --bssid 30:cb:c7:90:08:60 --as good
  python -m netsage.live.wifi_cli mark  --bssid a0:f8:49:85:fc:4e --as bad
  python -m netsage.live.wifi_cli watch --ssid F-VIT --interval 5 --loss-threshold 5
  python -m netsage.live.wifi_cli connect --bssid 30:cb:c7:90:08:60

`watch` measures this PC's link every few seconds; if the current AP is marked
BAD or its loss exceeds the threshold, it steers this PC to the best in-range
AP of the same SSID (a GOOD-marked AP always wins). Ctrl+C to stop.
"""
from __future__ import annotations

import argparse
import time

from .. import config
from . import winnet as w
from .apcard import APScorecard


def _ts():
    return time.strftime("%H:%M:%S")


def _scan(card, ssid):
    vis = set()
    for ap in w.scan(ssid):
        card.observe_scan(ap); vis.add(ap["bssid"])
    card.save()
    return vis


def _print_table(card, vis, cur):
    print(f"  {'BSSID':19s} {'quality':>7s} {'signal':>6s} {'loss':>6s} {'latency':>8s}  band / ch       mark")
    for r in card.ranked(vis or None):
        loss = "-" if r["loss"] is None else f"{r['loss']:.1f}%"
        lat = "-" if r["latency"] is None else f"{r['latency']:.0f}ms"
        here = "  <- connected" if r["bssid"] == cur else ""
        print(f"  {r['bssid']:19s} {r['quality']:7.1f} {str(r['signal'] or '-'):>5s}% {loss:>6s} {lat:>8s}  "
              f"{(r['band'] or '-'):8s} ch{r['channel'] or '-':4s} {r['mark'].upper():5s}{here}")


def cmd_scan(a):
    card = APScorecard(config.AP_SCORECARD_PATH)
    cur = w.current_interface()
    if w.location_blocked():
        print(w.LOCATION_HINT); return 1
    vis = _scan(card, a.ssid)
    if cur and cur.get("state") == "connected" and cur.get("ssid") == a.ssid:
        q = w.ping_summary(a.target, 6)
        card.observe_link(cur["bssid"], q["loss"], q["latency"]); card.save()
        print(f"Connected to {cur['ssid']} via {cur['bssid']}: {q['loss']:.0f}% loss, "
              f"{q['latency'] or '-'} ms")
    print(f"\nAccess points of {a.ssid} (best first):")
    _print_table(card, vis, cur and cur.get("bssid"))
    best = card.best(vis or None)
    if best:
        print(f"\nBest usable AP: {best['bssid']}")
    return 0


def cmd_mark(a):
    card = APScorecard(config.AP_SCORECARD_PATH)
    card.mark(a.bssid.lower(), getattr(a, "as"))
    print(f"Marked {a.bssid.lower()} as {getattr(a, 'as').upper()}.")
    return 0


def cmd_connect(a):
    cur = w.current_interface() or {}
    profile = cur.get("profile") or a.ssid
    print(w.connect_bssid(profile, a.bssid.lower()))
    return 0


def cmd_watch(a):
    card = APScorecard(config.AP_SCORECARD_PATH)
    print(f"Watching {a.ssid}: every {a.interval}s, steer away when loss > {a.loss_threshold}% "
          "or the AP is marked BAD.  Ctrl+C to stop.")
    try:
        while True:
            cur = w.current_interface()
            vis = _scan(card, a.ssid)
            if not cur or cur.get("state") != "connected" or cur.get("ssid") != a.ssid:
                best = card.best(vis)
                print(f"[{_ts()}] not connected to {a.ssid}"
                      + (f"; joining best AP {best['bssid']}" if best else "; no usable AP visible"))
                if best:
                    print("          ", w.connect_bssid(a.ssid, best["bssid"]))
                time.sleep(a.interval); continue
            q = w.ping_summary(a.target, a.count)
            card.observe_link(cur["bssid"], q["loss"], q["latency"]); card.save()
            mark = card.aps.get(cur["bssid"], {}).get("mark", "none")
            bad = q["loss"] > a.loss_threshold or mark == "bad"
            print(f"[{_ts()}] {cur['bssid']} [{mark.upper()}] loss={q['loss']:.0f}% "
                  f"lat={q['latency'] or '-'}ms signal={cur.get('signal')}% -> {'BAD' if bad else 'ok'}")
            if bad:
                best = card.best(vis, exclude={cur["bssid"]})
                if best:
                    print(f"[{_ts()}] steering {cur['bssid']} -> {best['bssid']} ({best['mark'].upper()})")
                    print("          ", w.connect_bssid(cur.get("profile") or a.ssid, best["bssid"]))
                else:
                    print(f"[{_ts()}] no better usable AP in range - staying")
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="wifi_cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("--ssid", default="F-VIT")
    s.add_argument("--target", default="8.8.8.8"); s.set_defaults(func=cmd_scan)
    m = sub.add_parser("mark"); m.add_argument("--bssid", required=True)
    m.add_argument("--as", choices=["good", "bad", "none"], required=True); m.set_defaults(func=cmd_mark)
    c = sub.add_parser("connect"); c.add_argument("--bssid", required=True)
    c.add_argument("--ssid", default="F-VIT"); c.set_defaults(func=cmd_connect)
    wt = sub.add_parser("watch"); wt.add_argument("--ssid", default="F-VIT")
    wt.add_argument("--target", default="8.8.8.8"); wt.add_argument("--count", type=int, default=4)
    wt.add_argument("--interval", type=int, default=5)
    wt.add_argument("--loss-threshold", type=float, default=5.0); wt.set_defaults(func=cmd_watch)
    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
