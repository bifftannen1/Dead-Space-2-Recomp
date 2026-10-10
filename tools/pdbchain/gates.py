"""Shared gate engine.

Every stage hands the engine its leave-one-out decisions on hidden known answers of the same
shape as the population it will fill: a list of (key, correct) for every decided case, plus the
count of a control that must fail (shifted target, shuffled labels, another function's
evidence...). The engine:

  * splits the decided keys into tune / held-out halves by a salted hash of the key (salt is
    recorded); a stage that tunes thresholds must tune on `tune` only and report `held` separately
  * reports precision, Wilson 95% lower bound, the held-out split's precision and bound, and the
    control rate
  * passes the gate only when precision >= bar on the whole decided set AND on the held-out split,
    the control rate is below the control bar, the decided count meets min_n, and every
    stage-specific requirement holds
  * logs every per-stage override of the default bars as a deviation in the receipt

A stage admits results only if its gate passed. The engine shakes itself when constructed: a
perfect synthetic decision list must pass and copies with 2% wrong decisions, with a 2% control,
and with an empty held-out split must each fail; otherwise the engine refuses to run.
"""
from __future__ import annotations

import hashlib

Z95 = 1.959964


def wilson_lower(k: int, n: int, z: float = Z95) -> float:
    if not n:
        return 0.0
    p = k / n
    return (p + z * z / (2 * n) - z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5) / (1 + z * z / n)


def split_of(key, salt: str) -> str:
    h = hashlib.sha256(("%s:%s" % (salt, key)).encode()).digest()
    return "tune" if h[0] % 2 == 0 else "held"


class GateEngine:
    def __init__(self, bar=0.99, control_bar=0.01, min_n=1, salt="pdbchain-split-v1",
                 overrides=None, require_heldout=True, wilson_bar=0.99):
        self.bar, self.control_bar, self.min_n, self.salt = bar, control_bar, min_n, salt
        self.wilson_bar = wilson_bar  # default for every stage (Wilson 95% lower bound, all decisions)
        self.overrides = overrides or {}
        self.require_heldout = require_heldout
        self.report = {}
        self.deviations = {}
        self.shake = self._shake()
        if not all(self.shake.values()):
            raise SystemExit("REFUSED: gate engine self-shake failed %s" % self.shake)
        self.report.clear()
        self.deviations.clear()

    def _shake(self):
        good = [(i, True) for i in range(2000)]
        bad = [(i, i % 50 != 0) for i in range(2000)]
        out = {"perfect_passes": self.judge("_shake_good", good, (2000, 0))["gate"],
               "two_percent_wrong_fails": not self.judge("_shake_bad", bad, (2000, 0))["gate"],
               "two_percent_control_fails": not self.judge("_shake_ctrl", good, (2000, 40))["gate"]}
        if self.require_heldout:
            tune_only = [(k, ok) for k, ok in good if split_of(k, self.salt) == "tune"]
            out["empty_heldout_fails"] = not self.judge("_shake_held", tune_only, (2000, 0))["gate"]
        if self.wilson_bar is not None or any("wilson" in o for o in self.overrides.values()):
            # a per-stage Wilson bar must refuse a perfect but too-small sample (300/300: lower 0.9874)
            self.overrides["_shake_wilson"] = {"wilson": 0.99}
            out["small_perfect_sample_fails_wilson_bar"] = not self.judge("_shake_wilson", good[:300], (2000, 0))["gate"]
            out["large_perfect_sample_passes_wilson_bar"] = self.judge("_shake_wilson", good, (2000, 0))["gate"]
            del self.overrides["_shake_wilson"]
        return out

    def bars(self, name):
        o = self.overrides.get(name, {})
        bar = o.get("precision", self.bar)
        cbar = o.get("control", self.control_bar)
        mn = o.get("min_n", self.min_n)
        self._wbar = o.get("wilson", self.wilson_bar) if not name.startswith("_shake") or "wilson" in o else None
        if o:
            self.deviations[name] = {"precision": bar, "control": cbar, "min_n": mn,
                                     "default": {"precision": self.bar, "control": self.control_bar, "min_n": self.min_n},
                                     "reason": o.get("reason", "")}
            if "wilson" in o:
                self.deviations[name]["wilson"] = self._wbar
        return bar, cbar, mn

    def judge(self, name, items, control=None, require=None, extra=None, heldout_items=None):
        """items: [(key, correct)] decided LOO cases. control: (population, accepted) or None
        (a stage with no possible control must say why in extra['control']). require: dict of
        stage-specific boolean conditions (all must hold). heldout_items: when the stage did its
        own tune/held split (threshold tuning), the held-out decisions; otherwise the engine
        splits `items` by salted hash."""
        bar, cbar, mn = self.bars(name)
        n = len(items)
        k = sum(1 for _, ok in items if ok)
        if heldout_items is None:
            held = [(x, ok) for x, ok in items if split_of(x, self.salt) == "held"]
            tune = [(x, ok) for x, ok in items if split_of(x, self.salt) == "tune"]
            split = {"salt": self.salt, "rule": "sha256(salt:key)[0] % 2"}
        else:
            held, tune = list(heldout_items), list(items)
            split = {"rule": "stage-defined (thresholds tuned on tune only)"}
        hk = sum(1 for _, ok in held if ok)
        tk = sum(1 for _, ok in tune if ok)
        prec = k / n if n else 0.0
        hprec = hk / len(held) if held else 0.0
        out = {"decided": n, "correct": k, "precision": round(prec, 5), "wilson95_lower": round(wilson_lower(k, n), 5),
               "tune": {"decided": len(tune), "correct": tk, "precision": round(tk / len(tune), 5) if tune else 0.0},
               "held": {"decided": len(held), "correct": hk, "precision": round(hprec, 5),
                        "wilson95_lower": round(wilson_lower(hk, len(held)), 5)},
               "split": split, "bar": bar, "control_bar": cbar, "min_n": mn}
        reasons = []
        if n < mn or n == 0:
            reasons.append("decided %d < min_n %d" % (n, mn))
        if prec < bar:
            reasons.append("precision %.5f < %.2f" % (prec, bar))
        wbar = self._wbar
        if wbar is not None:
            # all decisions: with a stage-defined held-out set that is tune + held
            wk, wn = (k + hk, n + len(held)) if heldout_items is not None else (k, n)
            wl = wilson_lower(wk, wn)
            out["wilson_bar"] = wbar
            out["wilson95_lower_all_decisions"] = {"decided": wn, "correct": wk, "wilson95_lower": round(wl, 5)}
            if wl < wbar:
                reasons.append("wilson95_lower %.5f (n=%d) < %.3f" % (wl, wn, wbar))
        if self.require_heldout and (not held or hprec < bar):
            reasons.append("held-out precision %.5f (n=%d) < %.2f" % (hprec, len(held), bar))
        if control is not None:
            cn, ca = control
            rate = ca / cn if cn else 1.0
            out["control"] = {"population": cn, "accepted": ca, "rate": round(rate, 5)}
            if not cn or rate >= cbar:
                reasons.append("control rate %.5f >= %.3f" % (rate, cbar))
        for key, ok in (require or {}).items():
            out.setdefault("requirements", {})[key] = bool(ok)
            if not ok:
                reasons.append("requirement failed: " + key)
        if extra:
            out.update(extra)
        out["gate"] = not reasons
        out["refusal_reasons"] = reasons
        self.report[name] = out
        return out


def unique(values):
    """Unique-resolution rule: the one value, or None when there are zero or several."""
    s = set(values)
    return next(iter(s)) if len(s) == 1 else None
