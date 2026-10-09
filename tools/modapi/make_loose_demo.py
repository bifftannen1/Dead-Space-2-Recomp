#!/usr/bin/env python3
r"""make_loose_demo.py -- build the sample loose-file mod (mods/loose_demo/files) from YOUR disc.

usage: python tools/modapi/make_loose_demo.py <disc.iso> [--mods mods]

The game opens game:\disc1_info.txt by path (it lists the chapters on each disc). This copies
it out of the user's own ISO and appends one CRLF, so the replacement is byte-different
(another size and hash) but means the same to the game. The result is game-derived, so it is
written to mods/loose_demo/files/ (gitignored), never committed. Set enabled = true in
mods/loose_demo.toml (or the staged copy) to use it; the loader then logs
  ds2-mods: loose_demo replaces game:\disc1_info.txt ...
  ds2-mods: game opened game:\disc1_info.txt (open) -> <host path> [loose_demo] <n> bytes fnv1a64=<h>
and this tool prints the size and fnv1a64 to match against.
Idempotent: a second run rewrites nothing and says so. Prints LOOSE_DEMO_OK <bytes> <fnv1a64>.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from disc_files import fnv1a64  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("iso")
    ap.add_argument("--mods", default=str(HERE.parents[1] / "mods"))
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "disc1_info.txt"
        p = subprocess.run([sys.executable, HERE / "disc_files.py", a.iso, "extract", "disc1_info.txt", raw],
                           capture_output=True, text=True)
        if p.returncode != 0:
            print("LOOSE_DEMO_FAIL", p.stdout.strip(), p.stderr.strip())
            return 1
        disc = raw.read_bytes()
    new = disc + b"\r\n"
    out = Path(a.mods) / "loose_demo" / "files" / "disc1_info.txt"
    if out.is_file() and out.read_bytes() == new:
        print(f"unchanged {out}")
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(new)
        print(f"written {out}")
    print(f"disc copy {len(disc)} bytes fnv1a64={fnv1a64(disc):016x}")
    print(f"LOOSE_DEMO_OK {len(new)} {fnv1a64(new):016x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
