"""Paths and tunable settings."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"

DIAGNOSER_PATH = MODELS_DIR / "diagnoser.npz"
PREDICTOR_PATH = MODELS_DIR / "predictor.npz"
METRICS_PATH = MODELS_DIR / "metrics.json"
AP_SCORECARD_PATH = DATA_DIR / "ap_scorecard.json"
FAULT_STATE_PATH = DATA_DIR / "live_fault_state.json"

# Engine
SIM_TICK_S = 1.0          # simulation step
LIVE_TICK_S = 1.5         # live probe interval
DIAG_WINDOW = 3           # ticks averaged for diagnosis
PRED_WINDOW = 10          # ticks used for trend features
PRED_HORIZON = 15         # "fault within the next N ticks"

# Self-healing
VERIFY_TICKS = 6          # ticks to wait before judging an action
CONFIRM_TICKS = 3         # consecutive faulty diagnoses before acting
RISK_ALERT = 0.70         # early-warning threshold for the predictor

# Safety constraints used by the digital twin
MAX_LINK_UTIL = 85.0      # %
MAX_CPU = 85.0            # %
SLA_LOSS = 2.0            # %
SLA_LATENCY = 100.0       # ms (internet path)
DISRUPTION_BUDGET = 60.0  # user-minutes an automatic action may cost
