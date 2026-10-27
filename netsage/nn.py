"""A small deep neural network written directly in NumPy.

Fully-connected layers, ReLU activations, softmax / sigmoid output, Adam
optimiser, L2 regularisation and mini-batch training. Written from scratch
(no TensorFlow/PyTorch) so it installs everywhere and every step -- including
the input gradients used for explanations -- is visible and auditable.
"""
from __future__ import annotations

import json

import numpy as np


class DeepNet:
    def __init__(self, sizes, task="multiclass", seed=0):
        """sizes: [n_in, h1, h2, ..., n_out]; task: 'multiclass' or 'binary'."""
        self.sizes = list(sizes)
        self.task = task
        rng = np.random.default_rng(seed)
        self.W, self.b = [], []
        for a, c in zip(sizes[:-1], sizes[1:]):
            self.W.append(rng.normal(0, np.sqrt(2.0 / a), (a, c)))   # He init
            self.b.append(np.zeros(c))
        self.meta = {}

    # ---------------------------------------------------------------- forward
    def _forward(self, X):
        acts, pre = [X], []
        h = X
        for i, (W, b) in enumerate(zip(self.W, self.b)):
            z = h @ W + b
            pre.append(z)
            if i < len(self.W) - 1:
                h = np.maximum(z, 0.0)
            else:
                h = self._out(z)
            acts.append(h)
        return acts, pre

    def _out(self, z):
        if self.task == "binary":
            return 1.0 / (1.0 + np.exp(-np.clip(z, -40, 40)))
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z)
        return e / e.sum(axis=1, keepdims=True)

    def predict_proba(self, X):
        X = np.atleast_2d(np.asarray(X, dtype=float))
        p = self._forward(X)[0][-1]
        return p[:, 0] if self.task == "binary" else p

    # --------------------------------------------------------------- training
    def fit(self, X, y, epochs=40, lr=2e-3, batch=128, l2=1e-4,
            class_weight=None, X_val=None, y_val=None, seed=1, verbose=False):
        rng = np.random.default_rng(seed)
        X = np.asarray(X, dtype=float)
        n = len(X)
        if self.task == "binary":
            Y = np.asarray(y, dtype=float).reshape(-1, 1)
        else:
            Y = np.eye(self.sizes[-1])[np.asarray(y, dtype=int)]
        sw = np.ones(n)
        if class_weight is not None:
            sw = np.asarray([class_weight[int(v)] for v in np.asarray(y).ravel()])
        mW = [np.zeros_like(w) for w in self.W]; vW = [np.zeros_like(w) for w in self.W]
        mb = [np.zeros_like(b) for b in self.b]; vb = [np.zeros_like(b) for b in self.b]
        b1, b2, eps, step = 0.9, 0.999, 1e-8, 0
        history = []
        for ep in range(epochs):
            order = rng.permutation(n)
            lr_ep = lr * (0.5 * (1 + np.cos(np.pi * ep / epochs)) * 0.9 + 0.1)   # cosine decay
            for s in range(0, n, batch):
                idx = order[s:s + batch]
                xb, yb, wb = X[idx], Y[idx], sw[idx][:, None]
                acts, pre = self._forward(xb)
                delta = (acts[-1] - yb) * wb / len(idx)      # softmax/sigmoid + CE
                step += 1
                for L in range(len(self.W) - 1, -1, -1):
                    gW = acts[L].T @ delta + l2 * self.W[L]
                    gb = delta.sum(axis=0)
                    if L > 0:
                        delta = (delta @ self.W[L].T) * (pre[L - 1] > 0)
                    mW[L] = b1 * mW[L] + (1 - b1) * gW; vW[L] = b2 * vW[L] + (1 - b2) * gW * gW
                    mb[L] = b1 * mb[L] + (1 - b1) * gb; vb[L] = b2 * vb[L] + (1 - b2) * gb * gb
                    c1, c2 = 1 - b1 ** step, 1 - b2 ** step
                    self.W[L] -= lr_ep * (mW[L] / c1) / (np.sqrt(vW[L] / c2) + eps)
                    self.b[L] -= lr_ep * (mb[L] / c1) / (np.sqrt(vb[L] / c2) + eps)
            rec = {"epoch": ep + 1, "loss": round(self.loss(X, y), 4)}
            if X_val is not None:
                rec["val_acc"] = round(self.accuracy(X_val, y_val), 4)
            history.append(rec)
            if verbose:
                print(rec)
        return history

    def loss(self, X, y):
        p = self.predict_proba(X)
        if self.task == "binary":
            p = np.clip(p, 1e-7, 1 - 1e-7)
            y = np.asarray(y, dtype=float)
            return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
        return float(-np.mean(np.log(np.clip(p[np.arange(len(y)), np.asarray(y, int)], 1e-7, 1))))

    def accuracy(self, X, y):
        p = self.predict_proba(X)
        pred = (p >= 0.5).astype(int) if self.task == "binary" else p.argmax(axis=1)
        return float(np.mean(pred == np.asarray(y).astype(int)))

    # ---------------------------------------------------- input gradients (XAI)
    def input_gradient(self, x, out_index=0):
        """d(output[out_index]) / d(input) for a single input vector."""
        acts, pre = self._forward(np.atleast_2d(x))
        p = acts[-1][0]
        if self.task == "binary":
            g = np.array([[p[0] * (1 - p[0])]])
        else:
            g = (p[out_index] * ((np.arange(len(p)) == out_index) - p))[None, :]
        for L in range(len(self.W) - 1, -1, -1):
            g = g @ self.W[L].T
            if L > 0:
                g = g * (pre[L - 1] > 0)
        return g[0]

    def integrated_gradients(self, x, baseline, out_index=0, steps=32):
        x = np.asarray(x, float); baseline = np.asarray(baseline, float)
        total = np.zeros_like(x)
        for a in (np.arange(steps) + 0.5) / steps:
            total += self.input_gradient(baseline + a * (x - baseline), out_index)
        return (x - baseline) * total / steps

    # -------------------------------------------------------------- persistence
    def save(self, path):
        arrs = {f"W{i}": w for i, w in enumerate(self.W)}
        arrs.update({f"b{i}": b for i, b in enumerate(self.b)})
        arrs["_cfg"] = np.array(json.dumps({"sizes": self.sizes, "task": self.task,
                                             "meta": self.meta}))
        np.savez_compressed(path, **arrs)

    @classmethod
    def load(cls, path):
        d = np.load(path, allow_pickle=False)
        cfg = json.loads(str(d["_cfg"]))
        net = cls(cfg["sizes"], cfg["task"])
        net.W = [d[f"W{i}"] for i in range(len(cfg["sizes"]) - 1)]
        net.b = [d[f"b{i}"] for i in range(len(cfg["sizes"]) - 1)]
        net.meta = cfg.get("meta", {})
        return net

    def n_params(self):
        return int(sum(w.size + b.size for w, b in zip(self.W, self.b)))
