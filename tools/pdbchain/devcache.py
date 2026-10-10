"""Development stage cache (`--dev-cache`): skip stages whose code and inputs did not change.

Never used by a real run or by regress_ds2.py (which always runs the full chain). A cache entry is
keyed by sha256 over the stage's source files, the whole config, and the stage's input digest; a
key miss recomputes and stores. A hit prints "DEV-CACHE HIT <stage>" so a log can never pass
for a full run, and the receipt records `dev_cache` (regress refuses a receipt with it).
"""
from __future__ import annotations

import hashlib, json, pickle
from pathlib import Path

HERE = Path(__file__).resolve().parent


def key(stage, sources, cfg, *digests):
    h = hashlib.sha256(stage.encode())
    for s in sources:
        h.update((HERE / s).read_bytes())
    h.update(json.dumps(cfg, sort_keys=True, default=str).encode())
    for d in digests:
        h.update(d if isinstance(d, bytes) else str(d).encode())
    return h.hexdigest()[:24]


def load(work, stage, k):
    p = Path(work) / "dev_cache" / ("%s_%s.pkl" % (stage, k))
    if p.is_file():
        print("DEV-CACHE HIT %s %s" % (stage, k))
        return pickle.loads(p.read_bytes())
    return None


def store(work, stage, k, obj):
    d = Path(work) / "dev_cache"
    d.mkdir(exist_ok=True)
    for old in d.glob("%s_*.pkl" % stage):
        old.unlink()
    (d / ("%s_%s.pkl" % (stage, k))).write_bytes(pickle.dumps(obj, protocol=4))
