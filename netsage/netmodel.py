"""Analytical model of an enterprise campus network.

`baseline_state()` describes the network (devices, links, user groups,
policies). `evaluate(state)` computes what every user group experiences:
routing, link load and queueing delay, device CPU, Wi-Fi airtime and
interference, DNS resolution, redundancy. The simulated "real" network and
the digital twin both use this model; the plant adds noise and hidden fault
dynamics, the twin uses it to run what-if experiments.
"""
from __future__ import annotations

import copy
import heapq

PROBE_GROUP = "Floor 1 East"
INET_RTT_EXTRA = 6.0     # ms from the provider edge to the probe target

_LAYOUT = {
    "INTERNET": (480, 45), "ISP-A": (350, 120), "ISP-B": (610, 120), "EDGE": (480, 200),
    "CORE-1": (340, 290), "CORE-2": (620, 290), "DC": (875, 220),
    "DNS-1": (755, 120), "DNS-2": (875, 120), "APP": (995, 120),
    "ACC-1": (310, 400), "ACC-2": (680, 400),
    "AP-1": (180, 500), "AP-2": (420, 500), "AP-3": (680, 500),
}


def _node(kind, label, **kw):
    d = {"kind": kind, "label": label, "admin_up": True, "oper_up": True,
         "cpu_base": 0.0, "cpu_storm": 0.0}
    d.update(kw)
    return d


def baseline_state():
    n = {
        "INTERNET": _node("cloud", "Internet"),
        "ISP-A": _node("isp", "ISP-A (primary, 1 Gb/s)"),
        "ISP-B": _node("isp", "ISP-B (backup, 500 Mb/s)"),
        "EDGE": _node("router", "Edge router / firewall", cpu_base=22.0),
        "CORE-1": _node("switch", "Core switch 1", cpu_base=18.0),
        "CORE-2": _node("switch", "Core switch 2", cpu_base=15.0),
        "DC": _node("switch", "Data-centre switch", cpu_base=12.0),
        "DNS-1": _node("dns", "DNS-1 (primary)", service_up=True),
        "DNS-2": _node("dns", "DNS-2 (secondary)", service_up=True),
        "APP": _node("server", "ERP application"),
        "ACC-1": _node("switch", "Access switch, floor 1", cpu_base=14.0),
        "ACC-2": _node("switch", "Access switch, floor 2", cpu_base=12.0),
        "AP-1": _node("ap", "AP-1  floor 1 east", channel=36, tx_boost=0, capacity=150, floor=1),
        "AP-2": _node("ap", "AP-2  floor 1 west", channel=149, tx_boost=0, capacity=150, floor=1),
        "AP-3": _node("ap", "AP-3  floor 2", channel=44, tx_boost=0, capacity=150, floor=2),
    }
    for k, (x, y) in _LAYOUT.items():
        n[k]["x"], n[k]["y"] = x, y
    links = {}
    for a, b, cap, lat, cost in [
        ("INTERNET", "ISP-A", 1000, 10.0, 1), ("INTERNET", "ISP-B", 500, 14.0, 1),
        ("ISP-A", "EDGE", 1000, 2.0, 1), ("ISP-B", "EDGE", 500, 3.0, 1),
        ("EDGE", "CORE-1", 10000, 0.3, 1), ("EDGE", "CORE-2", 10000, 0.3, 2),
        ("CORE-1", "DC", 10000, 0.2, 1), ("CORE-2", "DC", 10000, 0.2, 2),
        ("DC", "DNS-1", 1000, 0.1, 1), ("DC", "DNS-2", 1000, 0.1, 1), ("DC", "APP", 10000, 0.1, 1),
        ("CORE-1", "ACC-1", 1000, 0.3, 1), ("CORE-2", "ACC-1", 1000, 0.3, 2),
        ("CORE-1", "ACC-2", 1000, 0.3, 1), ("CORE-2", "ACC-2", 1000, 0.3, 2),
        ("ACC-1", "AP-1", 1000, 0.4, 1), ("ACC-1", "AP-2", 1000, 0.4, 1),
        ("ACC-2", "AP-3", 1000, 0.4, 1),
    ]:
        links[lid(a, b)] = {"a": a, "b": b, "cap": cap, "lat": lat, "cost": cost,
                            "admin_up": True, "oper_up": True, "loss_err": 0.0}
    groups = {
        "Floor 1 East": {"users": 120, "assoc": {"AP-1": 1.0},
                         "signal": {"AP-1": 78, "AP-2": 60, "AP-3": 38}},
        "Floor 1 West": {"users": 60, "assoc": {"AP-2": 1.0},
                         "signal": {"AP-2": 76, "AP-1": 62, "AP-3": 36}},
        "Floor 2":      {"users": 80, "assoc": {"AP-3": 1.0},
                         "signal": {"AP-3": 80, "AP-1": 34, "AP-2": 34}},
    }
    return {
        "nodes": n, "links": links, "groups": groups,
        "demand": {"business": 1.2, "bulk": 0.7, "app": 0.3, "backup": 0.0},
        "policy": {"isp_mode": "A", "qos_bulk_cap": None, "dns_server": "DNS-1", "copp": False},
        "ext_interference": {"channel": 36, "level": 0.0},
    }


def lid(a, b):
    return "|".join(sorted((a, b)))


def node_up(st, n):
    d = st["nodes"][n]
    return d["admin_up"] and d["oper_up"]


def link_up(st, k):
    L = st["links"][k]
    return L["admin_up"] and L["oper_up"] and node_up(st, L["a"]) and node_up(st, L["b"])


def _adj(st, exclude=()):
    adj = {n: [] for n in st["nodes"]}
    for k, L in st["links"].items():
        if k in exclude or not link_up(st, k):
            continue
        adj[L["a"]].append((L["b"], L["cost"], k))
        adj[L["b"]].append((L["a"], L["cost"], k))
    return adj


def shortest_path(st, src, dst, avoid_nodes=(), exclude=()):
    """Dijkstra on configured link costs. Returns (nodes, link_ids) or None."""
    if not node_up(st, src) or not node_up(st, dst):
        return None
    adj = _adj(st, exclude)
    dist = {src: 0}; prev = {}
    pq = [(0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == dst:
            break
        if d > dist.get(u, 1e18):
            continue
        for v, c, k in sorted(adj[u]):
            if v in avoid_nodes and v != dst:
                continue
            nd = d + c
            if nd < dist.get(v, 1e18):
                dist[v] = nd; prev[v] = (u, k); heapq.heappush(pq, (nd, v))
    if dst not in dist:
        return None
    nodes, lks = [dst], []
    while nodes[-1] != src:
        u, k = prev[nodes[-1]]
        nodes.append(u); lks.append(k)
    return nodes[::-1], lks[::-1]


def _edge_disjoint_paths(st, src, dst, limit=2):
    """Count edge-disjoint paths (unit max-flow), capped at `limit`."""
    adj = _adj(st)
    cap = {}
    for u in adj:
        for v, _, _ in adj[u]:
            cap[(u, v)] = cap.get((u, v), 0) + 1
    flow = 0
    while flow < limit:
        prev = {src: None}; q = [src]
        while q and dst not in prev:
            u = q.pop(0)
            for v, _, _ in adj[u]:
                if v not in prev and cap.get((u, v), 0) > 0:
                    prev[v] = u; q.append(v)
        if dst not in prev:
            break
        v = dst
        while prev[v] is not None:
            u = prev[v]; cap[(u, v)] -= 1; cap[(v, u)] = cap.get((v, u), 0) + 1; v = u
        flow += 1
    return flow


# ------------------------------------------------------------------ wireless
def ap_interference(st, ap):
    n = st["nodes"][ap]
    lvl = 0.0
    ext = st["ext_interference"]
    if ext["channel"] == n["channel"]:
        lvl += ext["level"]
    for other, o in st["nodes"].items():
        if other == ap or o["kind"] != "ap" or not node_up(st, other):
            continue
        if o.get("floor") == n.get("floor"):
            if o["channel"] == n["channel"]:
                lvl += 0.45
            lvl += 0.06 * o.get("tx_boost", 0)
    return min(1.0, lvl)


def ap_users(st, ap):
    return sum(g["users"] * f for g in st["groups"].values() for a, f in g["assoc"].items() if a == ap)


def wireless(st, group, ap):
    g = st["groups"][group]; n = st["nodes"][ap]
    interf = ap_interference(st, ap)
    load = ap_users(st, ap) / n["capacity"]
    sig = g["signal"].get(ap, 20) + 8 * n.get("tx_boost", 0)
    sig = max(0.0, min(100.0, sig * (1 - 0.8 * interf)))
    loss = interf * 28 + max(0.0, load - 1) * 10 + (max(0.0, 30 - sig) * 0.6)
    lat = 2.0 + interf * 25 + max(0.0, load - 0.8) * 60 + (25 if load > 1 else 0)
    return {"signal": sig, "loss": min(100.0, loss), "lat": lat, "load": load, "interference": interf}


# ------------------------------------------------------------------ evaluate
def evaluate(st):
    pol, dem = st["policy"], st["demand"]
    lan_avoid = ("INTERNET", "ISP-A", "ISP-B")
    flows = []   # (group|None, src, dst, mbps, cls)
    for gname, g in st["groups"].items():
        for ap, frac in g["assoc"].items():
            u = g["users"] * frac
            flows.append((gname, ap, "INTERNET", u * dem["business"], "business"))
            flows.append((gname, ap, "INTERNET", u * dem["bulk"], "bulk"))
            flows.append((gname, ap, "APP", u * dem["app"], "business"))
    if dem["backup"] > 0:
        flows.append((None, "APP", "INTERNET", dem["backup"], "bulk"))

    if pol["qos_bulk_cap"] is not None:
        bulk = sum(f[3] for f in flows if f[4] == "bulk")
        if bulk > pol["qos_bulk_cap"]:
            s = pol["qos_bulk_cap"] / bulk
            flows = [(a, b, c, d * s if e == "bulk" else d, e) for a, b, c, d, e in flows]

    isps = {"A": ["ISP-A"], "B": ["ISP-B"], "share": ["ISP-A", "ISP-B"]}[pol["isp_mode"]]
    isp_cap = {"ISP-A": 1000, "ISP-B": 500}
    load = {k: 0.0 for k in st["links"]}
    through = {n: 0.0 for n in st["nodes"]}
    routes = {}

    def route(src, dst):
        key = (src, dst)
        if key in routes:
            return routes[key]
        if dst != "INTERNET":
            p = shortest_path(st, src, dst, avoid_nodes=lan_avoid)
            r = [(1.0, p)] if p else []
        else:
            p = shortest_path(st, src, "EDGE", avoid_nodes=lan_avoid)
            r = []
            if p:
                ok = [i for i in isps if link_up(st, lid("EDGE", i)) and link_up(st, lid(i, "INTERNET"))]
                tot = sum(isp_cap[i] for i in ok)
                for i in ok:
                    r.append((isp_cap[i] / tot, (p[0] + [i, "INTERNET"],
                                                 p[1] + [lid("EDGE", i), lid(i, "INTERNET")])))
        routes[key] = r
        return r

    for g, src, dst, mbps, cls in flows:
        for share, (nodes, lks) in route(src, dst):
            for k in lks:
                load[k] += mbps * share
            for nd in nodes:
                through[nd] += mbps * share

    links = {}
    for k, L in st["links"].items():
        up = link_up(st, k)
        u = load[k] / L["cap"] if up else 0.0
        q = (1.5 * u / (1 - u) if u < 0.975 else 60.0) if up else 0.0
        q = min(q, 60.0)
        over = (1 - 1 / u) * 40 if u > 1 else 0.0
        links[k] = {"up": up, "load": round(load[k], 1), "util": round(100 * u, 1),
                    "delay": L["lat"] + q, "loss": min(100.0, L["loss_err"] + over),
                    "err": L["loss_err"]}
    nodes = {}
    for nname, nd in st["nodes"].items():
        storm = nd.get("cpu_storm", 0.0) * (0.15 if pol["copp"] else 1.0)
        cpu = min(100.0, nd["cpu_base"] + storm + through[nname] / 1000 * 6) if nd["cpu_base"] else 0.0
        delay = max(0.0, cpu - 85) * 1.5
        loss = max(0.0, cpu - 93) * 1.2
        nodes[nname] = {"up": node_up(st, nname), "cpu": round(cpu, 1), "delay": delay, "loss": loss}

    def path_kpi(nodes_, lks):
        lat = sum(links[k]["delay"] for k in lks) + sum(nodes[n]["delay"] for n in nodes_)
        keep = 1.0
        for k in lks:
            keep *= 1 - links[k]["loss"] / 100
        for n in nodes_:
            keep *= 1 - nodes[n]["loss"] / 100
        return 2 * lat, 100 * (1 - keep)

    def dns_ok(server, src, inet_ok, zone):
        if server == "PUBLIC":
            return zone == "external" and inet_ok
        if not node_up(st, server) or not st["nodes"][server].get("service_up", True):
            return False
        if not route(src, server):
            return False
        return inet_ok if zone == "external" else True

    groups = {}
    for gname, g in st["groups"].items():
        for ap, frac in g["assoc"].items():
            key = gname if len(g["assoc"]) == 1 else f"{gname} @ {ap}"
            res = {"group": gname, "ap": ap, "users": round(g["users"] * frac), "link_up": False,
                   "reach_inet": False, "reach_app": False, "dns_ext": False, "dns_int": False,
                   "gw_latency": None, "gw_loss": 100.0, "inet_latency": None, "inet_loss": 100.0,
                   "signal": 0.0, "max_util": 0.0, "max_cpu": 0.0, "gateway": None, "path": []}
            if node_up(st, ap):
                wl = wireless(st, gname, ap)
                res["signal"] = wl["signal"]; res["link_up"] = True
                r = route(ap, "INTERNET")
                if r:
                    lat_sum = loss_sum = 0.0; mu = mc = 0.0
                    for share, (nodes_, lks) in r:
                        la, lo = path_kpi(nodes_, lks)
                        lat_sum += share * la; loss_sum += share * lo
                        mu = max(mu, max(links[k]["util"] for k in lks))
                        mc = max(mc, max(nodes[n]["cpu"] for n in nodes_))
                    res["path"] = r[0][1][0]
                    wl_keep = 1 - wl["loss"] / 100
                    res["inet_latency"] = lat_sum + 2 * wl["lat"] + INET_RTT_EXTRA
                    res["inet_loss"] = 100 * (1 - wl_keep * (1 - loss_sum / 100))
                    res["reach_inet"] = res["inet_loss"] < 90
                    res["max_util"], res["max_cpu"] = mu, mc
                p_edge = shortest_path(st, ap, "EDGE", avoid_nodes=lan_avoid)
                if p_edge:
                    nodes_, lks = p_edge
                    core = next((x for x in nodes_ if x.startswith("CORE")), nodes_[-1])
                    i = nodes_.index(core)
                    la, lo = path_kpi(nodes_[:i + 1], lks[:i])
                    res["gateway"] = core
                    res["gw_latency"] = la + 2 * wl["lat"]
                    res["gw_loss"] = 100 * (1 - (1 - wl["loss"] / 100) * (1 - lo / 100))
                    if not r:
                        res["max_util"] = max([links[k]["util"] for k in lks[:i]] or [0])
                        res["max_cpu"] = max(nodes[n]["cpu"] for n in nodes_[:i + 1])
                ra = route(ap, "APP")
                res["reach_app"] = bool(ra)
                res["dns_ext"] = dns_ok(pol["dns_server"], ap, res["reach_inet"], "external")
                res["dns_int"] = dns_ok(pol["dns_server"], ap, res["reach_inet"], "internal")
            groups[key] = res

    redundant = {}
    for acc in ("ACC-1", "ACC-2"):
        redundant[acc] = _edge_disjoint_paths(st, acc, "EDGE") >= 2

    aps = {}
    for ap, nd in st["nodes"].items():
        if nd["kind"] == "ap":
            aps[ap] = {"up": node_up(st, ap), "users": round(ap_users(st, ap)),
                       "load": round(100 * ap_users(st, ap) / nd["capacity"], 1),
                       "interference": round(ap_interference(st, ap), 2), "channel": nd["channel"]}
    isp_load = {i: links[lid(i, "INTERNET")]["util"] for i in ("ISP-A", "ISP-B")}
    return {"links": links, "nodes": nodes, "groups": groups, "aps": aps,
            "redundant": redundant, "isp_util": isp_load, "probe": probe_view(st, groups)}


def probe_view(st, groups):
    """What the monitoring probe (a client in PROBE_GROUP) measures."""
    g = st["groups"][PROBE_GROUP]
    ap = max(g["assoc"], key=g["assoc"].get)
    key = PROBE_GROUP if len(g["assoc"]) == 1 else f"{PROBE_GROUP} @ {ap}"
    return dict(groups[key])


def clone(st):
    return copy.deepcopy(st)
