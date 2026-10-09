#!/usr/bin/env python3
"""Short automated launch of the built game: start, capture the window, close it.

usage: python tools/smoke_test.py <ds2.exe> <out_dir> [--iso PATH] [--at 20,45,70] [--user-root DIR]

Refuses to start if a ds2.exe is already running (tasklist). Captures the window with
PrintWindow(PW_RENDERFULLCONTENT) at each --at second as frame_<s>.png -- no focus change, no
screen grab, so it works with the window behind others. Records per-frame stats (mean luma, share
of non-black pixels, distinct colours in a 64x36 thumbnail). Closes with WM_CLOSE and waits.
Prints SMOKE_RENDERED only if the process survived to the last capture, the last frame is not
blank (non-black share >= 5% and >= 16 distinct colours), the process exited after WM_CLOSE and
tasklist shows no ds2.exe afterwards; exits 1 otherwise.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except OSError:
    user32.SetProcessDPIAware()
WM_CLOSE = 0x0010
PW_RENDERFULLCONTENT = 2


def running(names=("ds2.exe",)) -> list[str]:
    out = subprocess.run(["tasklist"], capture_output=True, text=True).stdout.lower()
    return [l for l in out.splitlines() if any(l.startswith(n) for n in names)]


def window_of(pid: int):
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def print_window(hwnd):
    """Client area via PrintWindow into a memory DC (window may be occluded; focus untouched)."""
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right, r.bottom
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    ok = user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT | 1)  # 1 = PW_CLIENTONLY
    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    if not ok:
        return None
    return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)


def stats(img) -> dict:
    g = img.convert("L")
    px = list(g.getdata())
    thumb = img.convert("RGB").resize((64, 36))
    return {"w": img.width, "h": img.height, "mean_luma": round(sum(px) / len(px), 2),
            "nonblack_share": round(sum(1 for v in px if v > 16) / len(px), 4),
            "distinct_thumb_colours": len(set(thumb.getdata()))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("exe")
    ap.add_argument("out")
    ap.add_argument("--iso")
    ap.add_argument("--user-root")
    ap.add_argument("--at", default="20,45,70")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    busy = running()
    if busy:
        print("SMOKE_REFUSED already running:", busy)
        return 1
    argv = [a.exe]
    if a.iso:
        argv.append(f"--ds2_iso={a.iso}")
    if a.user_root:
        argv.append(f"--user_data_root={a.user_root}")
    times = sorted(int(t) for t in a.at.split(","))
    t0 = time.monotonic()
    proc = subprocess.Popen(argv, cwd=str(Path(a.exe).parent))
    rec = {"argv": argv, "pid": proc.pid, "frames": [], "capture": "PrintWindow(PW_RENDERFULLCONTENT|PW_CLIENTONLY)"}
    hwnd = None
    for t in times:
        while time.monotonic() - t0 < t and proc.poll() is None:
            time.sleep(0.25)
        if proc.poll() is not None:
            rec["early_exit"] = {"t": round(time.monotonic() - t0, 1), "rc": proc.returncode}
            break
        hwnd = hwnd or window_of(proc.pid)
        img = print_window(hwnd) if hwnd else None
        if img is None:
            rec["frames"].append({"t": t, "error": "no window" if not hwnd else "PrintWindow failed"})
            continue
        f = out / f"frame_{t:03d}.png"
        img.save(f)
        s = stats(img)
        s.update({"t": t, "file": f.name})
        rec["frames"].append(s)
        print("FRAME", json.dumps(s), flush=True)
    alive_at_end = proc.poll() is None
    closed = False
    if alive_at_end and hwnd:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        try:
            proc.wait(timeout=30)
            closed = True
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    after = running()
    rec.update({"alive_at_last_capture": alive_at_end, "exited_after_close": closed,
                "rc": proc.returncode, "still_running_after": after})
    (out / "smoke.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
    last = next((f for f in reversed(rec["frames"]) if "mean_luma" in f), None)
    ok = (alive_at_end and closed and not after and last is not None and last["t"] == times[-1]
          and last["nonblack_share"] >= 0.05 and last["distinct_thumb_colours"] >= 16)
    print("alive", alive_at_end, "closed", closed, "rc", proc.returncode, "tasklist_after", after)
    print("SMOKE_RENDERED" if ok else "SMOKE_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
