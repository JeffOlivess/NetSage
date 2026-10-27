"""NetSage web server.   Run:  python app.py   then open http://127.0.0.1:5000"""
from __future__ import annotations

import os
import sys

import numpy as np
from flask import Flask, jsonify, render_template, request
from flask.json.provider import DefaultJSONProvider

from netsage import PROJECT_TITLE
from netsage.config import WEB_DIR
from netsage.engine import Engine



class _JSON(DefaultJSONProvider):
    @staticmethod
    def default(o):
        if isinstance(o, np.generic):
            return o.item()
        return DefaultJSONProvider.default(o)


app = Flask(__name__, template_folder=str(WEB_DIR / "templates"), static_folder=str(WEB_DIR / "static"))
app.json = _JSON(app)
engine = Engine()


def body():
    return request.get_json(silent=True) or {}


@app.get("/")
def index():
    return render_template("index.html", title=PROJECT_TITLE)


@app.get("/api/state")
def state():
    return jsonify(engine.snapshot())


@app.get("/api/model")
def model():
    return jsonify(engine.model_info())


@app.post("/api/mode")
def mode():
    m = body().get("mode")
    if m not in ("sim", "live"):
        return jsonify({"ok": False, "error": "mode must be sim or live"}), 400
    return jsonify(engine.set_mode(m))


@app.post("/api/inject")
def inject():
    b = body()
    return jsonify(engine.inject(b.get("kind"), int(b.get("seconds") or 60)))


@app.post("/api/clear")
def clear():
    return jsonify(engine.clear())


@app.post("/api/settings")
def settings():
    b = body()
    return jsonify(engine.settings(b.get("auto"), b.get("predictive")))


@app.post("/api/approve")
def approve():
    return jsonify(engine.approve(body().get("action_id")))


@app.get("/api/whatif")
def whatif():
    return jsonify(engine.whatif(request.args.get("code", "")))


@app.post("/api/ap/mark")
def ap_mark():
    b = body()
    if b.get("mark") not in ("good", "bad", "none"):
        return jsonify({"ok": False, "error": "mark must be good, bad or none"}), 400
    return jsonify(engine.ap_mark(str(b.get("bssid", "")).lower(), b["mark"]))


@app.post("/api/ap/connect")
def ap_connect():
    return jsonify(engine.ap_connect(str(body().get("bssid", "")).lower()))


@app.post("/api/ap/rescan")
def ap_rescan():
    if engine.live:
        engine.live["mon"].rescan()
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "127.0.0.1")
    if "--live" in sys.argv:
        engine.set_mode("live")
    print(f"\n  NetSage is running:  http://{host}:{port}\n  Press Ctrl+C to stop.\n")
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
