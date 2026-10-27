# NetSage

**An Explainable, Self-Healing Deep Learning Framework for Enterprise Network Fault Diagnosis, Prediction and Recovery**

NetSage watches a network, works out what is wrong with it (diagnosis), warns before a fault
happens (prediction), explains its reasoning (explainable AI) and repairs the fault on its own
(self-healing). Every repair is first tried on a **digital twin** of the network, so only actions
that are proven safe ever touch the real network.

---

## How to run (Windows)

1. Install **Python 3.9 or newer** from python.org. Tick **"Add python.exe to PATH"** during setup.
2. Download this repository (Code > Download ZIP) and unzip it somewhere easy, e.g. `Downloads\NetSage`.
3. Double-click **`Start-NetSage.bat`** and click **Yes** on the administrator prompt.
   The first run creates a `.venv` folder and installs Flask, NumPy and psutil (about a minute).
4. Your browser opens **http://127.0.0.1:5000**. Keep the black NetSage window open; close it to stop.

Manual alternative (any OS): `pip install -r requirements.txt` then `python app.py`.
Run the tests with `python -m unittest discover -s tests -v`.

If the window is killed in the middle of a live fault and the internet stays broken, run
**`Fix-Network.bat`**.

---

## Demo script

**Simulation mode** (an enterprise campus: 15 devices, 3 Wi-Fi access points, 260 users)

| Button | What happens | Diagnosis | Twin-approved fix | Rejected by the twin |
|---|---|---|---|---|
| Cloud backup saturates ISP-A | gradual | Congestion | QoS: police bulk traffic | move everything to ISP-B (overloads it) |
| Broadcast storm overloads CORE-1 | gradual | Device overload | control-plane policing | shut CORE-1 (removes redundancy) |
| Fibre errors on CORE-1 to ACC-1 | gradual | Packet loss | drain traffic to the redundant link | shut link (single point of failure), bounce (errors persist) |
| Radio interference on AP-1 | gradual | Wireless degraded | move AP-1 to a clean channel | channel 149 (clashes with AP-2), max TX power |
| ISP-A provider outage | abrupt | Upstream outage | fail over to ISP-B **with** QoS | plain failover (ISP-B at 99%), reboot edge router |
| DNS-1 service crash | abrupt | DNS failure | fail clients over to DNS-2 | public DNS (breaks internal names), restart (too disruptive) |
| AP-1 radio failure | abrupt | Link down | split clients across AP-2 and AP-3 | all clients to AP-2 (over capacity) |

* **Predictive ON** - gradual faults are usually caught *before* they happen and the incident is
  marked **PREVENTED**.
* **Predictive OFF** - the fault happens, is diagnosed, and the incident is marked **RECOVERED**.
* **Auto-heal OFF** - NetSage stops at the recommendation and waits for you to click *Approve*.
* **Digital Twin** tab - the live topology and the table of every candidate action with its
  verdict (RECOMMENDED / SAFE / UNSAFE / INEFFECTIVE / BLOCKED) and the reasons. Use
  *What-if* to test any playbook against the current network.
* **Explainability** tab - which measurements drove the diagnosis (integrated gradients),
  class probabilities, plain-language evidence and what is raising the fault risk.
* **Model** tab - architecture, accuracy, precision/recall and the confusion matrix.

**Live Wi-Fi mode** (this PC's real connection)

The same deep networks read real measurements every 1.5 s: gateway ping, internet ping, a real
DNS query, Wi-Fi signal, throughput and CPU. The fault buttons cause **real** faults on this PC
only (firewall rules, disconnect, adapter off, bulk downloads); each is undone automatically.
The live digital twin models *this PC -> access point -> gateway -> internet / DNS* and picks a
fix, e.g. remove the blocking rule, re-enable the adapter, reconnect, roam to a better access point.

The **Access Points** tab lists every BSSID of your SSID with signal, loss and latency. Mark one
**Good** to always prefer it or **Bad** to never use it; with Auto-heal on, NetSage steers away
from a Bad AP automatically.

Windows 11: turn on **Settings > Privacy & security > Location** and *Let desktop apps access
your location*, otherwise Windows hides Wi-Fi details.

Command-line AP tool (also used by `Run-FVIT-Watch.bat`):

```
python -m netsage.live.wifi_cli scan  --ssid F-VIT
python -m netsage.live.wifi_cli mark  --bssid 30:cb:c7:90:08:60 --as good
python -m netsage.live.wifi_cli watch --ssid F-VIT --interval 5
```

## Dataset

The exact training and test data are in `dataset/` as CSV files (open them in Excel):

* `diagnosis_dataset.csv` - 24,000 labelled samples (12 features + fault class), with the
  train/test split and the trained model's prediction for each row (99.56% match on the test rows)
* `timeseries_dataset.csv` - 300 telemetry time series used for fault prediction
* `README.txt` - column descriptions

Regenerate them with `python scripts/export_dataset.py`.

## Paper

The IEEE-format paper and the project report are in `docs/`.

## Team

Jifano Oliver J P (24BCE2742), Hrithikesh Vineeth Nair (24BAI0266), T. Nitin Yajatkumar (24BCE0571)
Computer Networks - School of Computer Science and Engineering, VIT Vellore. Guide: Arivoli A.

---

## Architecture

```
 telemetry (12 features)          deep learning                    digital twin                 action
 ----------------------    ----------------------------    ----------------------------    ---------------
 simulated campus  ----+-> Diagnoser 12-64-64-32-8 ---+--> sync with network state  ---> execute safest
 or live Wi-Fi probes  |   (8 fault classes, softmax)  |    run every candidate on a        action
                       |                               |    copy, check 7 safety rules      verify, roll back
                       +-> Predictor 24-64-32-1  ------+    project trends for early        or escalate
                           (risk of fault in ~15 s)         warnings
                                   |
                           integrated gradients + evidence  ->  explanations in the dashboard
```

* **Deep networks** (`netsage/nn.py`) - multilayer perceptrons written from scratch in NumPy:
  He initialisation, ReLU, softmax/sigmoid, Adam with cosine learning-rate decay, L2.
  Trained on synthetic telemetry with randomised baselines (2 ms LAN to 120 ms hotspot),
  realistic fault signatures, measurement noise and overlapping cases. Retrain with
  `python scripts/train.py`.
* **Explainability** - integrated gradients from a healthy reference link for every diagnosis,
  plus rule-based evidence sentences, plus the twin's per-action reasons.
* **Prediction** - a second network reads the last 10 samples (levels + trends) and outputs the
  probability of a fault within the next ~15 samples; a robust (Theil-Sen) extrapolation tells
  which fault is coming.
* **Digital twin** (`netsage/twin.py`, `netsage/netmodel.py`) - an analytical model of the
  network: routing (Dijkstra), link load and queueing delay, device CPU, Wi-Fi airtime and
  interference, DNS zones, redundancy (edge-disjoint paths). It is synchronised every second and
  reports its fidelity (predicted vs measured latency).
* **Self-healing** (`netsage/healer.py`) - confirm (3 samples) -> plan on the twin -> execute ->
  verify (6 samples) -> roll back and try the next safe action, or escalate. If the diagnosed
  playbook has no safe fix, the twin searches the other playbooks in probability order.

### Safety constraints checked by the twin

1. Every user group keeps internet, ERP and DNS (internal and external) reachability
2. Packet loss <= 2% and latency <= 100 ms for all groups
3. No link above 85% utilisation
4. No device CPU above 85%
5. No access point over capacity or on a clashing channel
6. No access switch loses its redundant uplink
7. Disruption within 60 user-minutes

---

## Project structure

```
app.py                  Flask server and REST API
netsage/
  features.py           12 telemetry features, 8 fault classes
  nn.py                 deep neural network (NumPy)
  dataset.py            synthetic training data
  models.py             Diagnoser + Predictor + explanations
  netmodel.py           analytical enterprise network model
  simulation.py         simulated production network with faults
  twin.py               digital twin: what-if evaluation and projection
  playbooks.py          recovery actions per fault class
  healer.py             closed-loop self-healing
  engine.py             runtime loop tying everything together
  live/                 Windows live mode: probes, faults, live twin, AP scorecard, CLI
web/                    dashboard (HTML, CSS, JS)
models/                 trained networks + metrics
tests/                  unit and end-to-end tests
```

## Honest limits

* The simulated campus is a model, not a real enterprise network; it exists to show the full
  closed loop safely. Live mode acts only on this PC.
* Live faults are created on this PC by NetSage itself so they can be demonstrated and undone;
  the live twin only ever removes rules NetSage created.
* Forcing an access point uses the Windows Native Wi-Fi API; the Wi-Fi driver can still roam on
  its own later.
