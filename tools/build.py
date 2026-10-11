#!/usr/bin/env python3
"""Build ds2.exe from this source tree. Windows; VS 2022 BuildTools (clang), CMake, Ninja, git.

usage: python tools/build.py [--xex default.xex | --iso disc1.iso] [--sdk-prefix DIR]
                             [--sdk-dir DIR] [--build-dir DIR] [--jobs N] [--mods]

Steps (each stops with BUILD_FAIL and exit 1 when its postcondition fails):
  1 sdk  (skipped with --sdk-prefix) fetch rexglue-sdk at the pinned commit (sdk/REXGLUE_BASE.txt)
         with submodules into thirdparty/rexglue-sdk, apply sdk/rexglue_ds2.patch (sha256 checked),
         configure + build + install it (Ninja, Release, clang, Vulkan on, D3D12/Tracy off).
  2 app  configure + build this folder's CMakeLists.txt -> <build-dir>/bin/ds2.exe (+ rexruntime.dll,
         rexgpu-xenos.dll). No codegen and no organizer run: src/ is compiled as checked in.
  3 xex  (with --xex/--iso) stage your own default.xex (sha256 pinned) as bin/game/default.xex and,
         with --iso, write bin/ds2.toml naming the ISO.
Prints BUILD_OK <exe> only when the three binaries exist.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UPSTREAM = "https://github.com/rexglue/rexglue-sdk.git"
VCVARS = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat")
XEX_SHA256 = "928597ddb80240b8aefeef0a70a9476ee85d6de733035b326dd155c2094a3408"
SDK_OPTIONS = ["-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++",
               "-DCMAKE_C_FLAGS=-march=x86-64-v2", "-DCMAKE_CXX_FLAGS=-march=x86-64-v2",
               "-DCMAKE_CXX_STANDARD=23", "-DREXGLUE_USE_VULKAN=ON", "-DREXGLUE_USE_D3D12=OFF",
               "-DREXGLUE_ENABLE_TRACY=OFF", "-DREXGLUE_BUILD_TESTS=OFF"]


def fail(msg: str) -> None:
    print("BUILD_FAIL", msg, flush=True)
    sys.exit(1)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def base_pin() -> tuple[str, str]:
    kv = {}
    for line in (REPO / "sdk" / "REXGLUE_BASE.txt").read_text(encoding="utf-8").splitlines():
        k, _, v = line.partition("=")
        if v:
            kv[k.strip()] = v.strip()
    return kv["upstream_commit"], kv["patch_sha256"]


def run(argv, cwd=None, env=None, log: Path | None = None, check=True) -> int:
    print("+", " ".join(str(a) for a in argv), flush=True)
    t = time.monotonic()
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("wb") as f:
            rc = subprocess.run([str(a) for a in argv], cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT).returncode
    else:
        rc = subprocess.run([str(a) for a in argv], cwd=cwd, env=env).returncode
    print(f"  rc={rc} {time.monotonic() - t:.0f}s" + (f" log={log}" if log else ""), flush=True)
    if check and rc != 0:
        fail(f"{argv[0]} rc={rc}" + (f" (see {log})" if log else ""))
    return rc


def vc_env(scratch: Path) -> dict:
    if not VCVARS.is_file():
        fail(f"VS 2022 BuildTools not found ({VCVARS})")
    # A .cmd file, not an inline `cmd /c`: Python's argv quoting breaks the quoted path.
    script = scratch / "capture-vcvars.cmd"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f'@echo off\r\ncall "{VCVARS}" >nul\r\nif errorlevel 1 exit /b %errorlevel%\r\nset\r\n',
                      encoding="ascii")
    out = subprocess.run(["cmd", "/d", "/c", str(script)], capture_output=True, text=True)
    env = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line and not line.startswith("="))
    if out.returncode != 0 or "INCLUDE" not in env:
        fail("vcvars64 failed")
    return env


def git(sdk: Path, *a, check=True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-C", str(sdk), *a], capture_output=True, text=True)
    if check and p.returncode != 0:
        fail(f"git {' '.join(a)}: {p.stderr.strip()[-400:]}")
    return p


def materialize_symlinks(sdk: Path) -> None:
    """Git for Windows checks out symlinks (mode 120000) as text files holding the target path,
    which do not compile. Replace each one with a copy of its target inside the same repository.
    Idempotent."""
    repos = [sdk] + [p.parent for p in sdk.rglob(".git") if p.parent != sdk]
    done = pending = 0
    for _ in range(4):
        pending = 0
        for repo in repos:
            for line in git(repo, "ls-files", "-s").stdout.splitlines():
                meta, _, rel = line.partition("\t")
                if not meta.startswith("120000"):
                    continue
                link = repo / rel
                if not link.is_file() or link.stat().st_size > 4096:
                    continue
                text = link.read_bytes().decode("utf-8", "replace").strip()
                target = (link.parent / text).resolve()
                if "\n" in text or not text or repo.resolve() not in target.parents:
                    continue
                if target.is_file() and target.read_bytes().strip() != text.encode():
                    link.write_bytes(target.read_bytes())
                    done += 1
                elif target.is_dir():
                    link.unlink()
                    shutil.copytree(target, link)
                    done += 1
                else:
                    pending += 1
        if not pending:
            break
    print(f"symlinks: materialized {done}, dangling (target not in checkout) {pending}")


def step_sdk_source(sdk: Path, commit: str, patch_sha: str) -> None:
    patch = REPO / "sdk" / "rexglue_ds2.patch"
    if sha256(patch) != patch_sha:
        fail(f"sdk/rexglue_ds2.patch sha256 {sha256(patch)} != pinned {patch_sha}")
    if not (sdk / ".git").exists():
        sdk.mkdir(parents=True, exist_ok=True)
        git(sdk, "init", "-q")
        git(sdk, "remote", "add", "origin", UPSTREAM)
        run(["git", "-C", sdk, "fetch", "--depth", "1", "origin", commit])
        git(sdk, "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD")
    head = git(sdk, "rev-parse", "HEAD").stdout.strip()
    if head != commit:
        fail(f"{sdk} HEAD {head} != pinned {commit}")
    run(["git", "-C", sdk, "submodule", "update", "--init", "--recursive", "--depth", "1", "--jobs", "8"])
    materialize_symlinks(sdk)
    if git(sdk, "apply", "--check", "--reverse", "--whitespace=nowarn", str(patch), check=False).returncode == 0:
        print("patch: already applied (reverse --check clean)")
        return
    fwd = git(sdk, "apply", "--check", "--whitespace=nowarn", str(patch), check=False)
    if fwd.returncode != 0:
        fail("rexglue_ds2.patch neither applies nor reverse-applies: " + fwd.stderr.strip()[-600:])
    git(sdk, "apply", "--whitespace=nowarn", str(patch))
    if git(sdk, "apply", "--check", "--reverse", "--whitespace=nowarn", str(patch), check=False).returncode != 0:
        fail("patch applied but does not reverse-check")
    print("patch: applied")


def step_sdk_build(sdk: Path, env: dict, jobs: int, logs: Path) -> Path:
    bt, inst = sdk / "out" / "build" / "ds2-release", sdk / "out" / "install" / "ds2-release"
    run(["cmake", "-S", sdk, "-B", bt, "-G", "Ninja", *SDK_OPTIONS, f"-DCMAKE_INSTALL_PREFIX={inst}"],
        env=env, log=logs / "sdk_configure.log")
    run(["cmake", "--build", bt, "--target", "install", "-j", str(jobs)], env=env, log=logs / "sdk_build.log")
    return inst


def check_sdk(inst: Path) -> None:
    need = [inst / "bin" / "rexruntime.dll", inst / "bin" / "rexgpu-xenos.dll",
            inst / "lib" / "cmake" / "rexglue"]
    missing = [str(p) for p in need if not p.exists()]
    if missing:
        fail(f"SDK install at {inst} lacks {missing}")


XEXP_SHA256 = "8d386d37e8f71937b9d9c4b34bdbf85240101479c8aaece1002d94e7cdc01ec2"  # Title Update #2, disc001/default.xexp


def step_xexp(bindir: Path, target: str, xexp: str | None) -> None:
    """tu2: stage the pinned TU2 patch next to default.xex. base: remove any staged patch (ds2.exe
    refuses to run a base build with one)."""
    dst = bindir / "game" / "default.xexp"
    if target != "tu2":
        if dst.exists():
            os.chmod(dst, 0o666); dst.unlink(); print("xexp: removed", dst, "(base target)")
        return
    if not xexp:
        fail("--target tu2 needs --xexp <Title Update #2 disc001/default.xexp>")
    got = sha256(Path(xexp))
    if got != XEXP_SHA256:
        fail(f"default.xexp sha256 {got} != pinned TU2 {XEXP_SHA256}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(xexp, dst)
    print("xexp: staged", dst, got)


def step_app(inst: Path, env: dict, jobs: int, bt: Path, mods: bool, target: str = "base") -> Path:
    bindir = bt / "bin"
    run(["cmake", "-S", REPO, "-B", bt, "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
         "-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++", "-DCMAKE_CXX_FLAGS=-march=x86-64-v2",
         f"-DCMAKE_PREFIX_PATH={inst}", f"-DCMAKE_RUNTIME_OUTPUT_DIRECTORY={bindir}",
         f"-DDS2_MODS={'ON' if mods else 'OFF'}", f"-DDS2_TARGET={target}"],
        env=env, log=bt / "logs" / "configure.log")
    run(["cmake", "--build", bt, "-j", str(jobs)], env=env, log=bt / "logs" / "build.log")
    exe = bindir / "ds2.exe"
    missing = [p.name for p in (exe, bindir / "rexruntime.dll", bindir / "rexgpu-xenos.dll") if not p.is_file()]
    if missing:
        fail(f"app build lacks {missing} in {bindir}")
    return exe


def step_xex(bindir: Path, xex: str | None, iso: str | None) -> None:
    game = bindir / "game"
    game.mkdir(parents=True, exist_ok=True)
    dst = game / "default.xex"
    if dst.exists():
        os.chmod(dst, 0o666)
        dst.unlink()
    if xex:
        shutil.copyfile(xex, dst)
    else:
        run([sys.executable, REPO / "tools" / "extract_xex.py", iso, dst])
    got = sha256(dst)
    if got != XEX_SHA256:
        fail(f"default.xex sha256 {got} != pinned {XEX_SHA256} (src/ was recompiled from that exact XEX)")
    print("xex: staged", dst, got)
    toml = bindir / "ds2.toml"
    if iso and not toml.exists():
        toml.write_text(f"# Written by tools/build.py; edit freely.\nds2_iso = '{Path(iso).resolve()}'\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xex")
    ap.add_argument("--iso")
    ap.add_argument("--sdk-dir", default=str(REPO / "thirdparty" / "rexglue-sdk"))
    ap.add_argument("--sdk-prefix", help="an already built + installed patched SDK (skips step 1)")
    ap.add_argument("--build-dir", default=str(REPO / "build"))
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 8)
    ap.add_argument("--mods", action="store_true", help="DS2_MODS=ON (mod API + mods/)")
    ap.add_argument("--target", choices=("base", "tu2"), default="base",
                    help="base: src/ (retail XEX); tu2: src_tu2/ (with Title Update #2, needs --xexp)")
    ap.add_argument("--xexp", help="Title Update #2 disc001/default.xexp (required for --target tu2)")
    a = ap.parse_args()
    if a.target == "tu2" and not a.xexp:
        fail("--target tu2 needs --xexp <Title Update #2 disc001/default.xexp>")
    for p in (a.xex, a.iso, a.xexp):
        if p and not Path(p).is_file():
            fail(f"not a file: {p}")
    bt = Path(a.build_dir).resolve()
    env = vc_env(bt)
    t = time.monotonic()
    if a.sdk_prefix:
        inst = Path(a.sdk_prefix).resolve()
    else:
        commit, patch_sha = base_pin()
        sdk = Path(a.sdk_dir).resolve()
        step_sdk_source(sdk, commit, patch_sha)
        inst = step_sdk_build(sdk, env, a.jobs, bt / "logs")
    check_sdk(inst)
    exe = step_app(inst, env, a.jobs, bt, a.mods, a.target)
    if a.xex or a.iso:
        step_xex(exe.parent, a.xex, a.iso)
    step_xexp(exe.parent, a.target, a.xexp)
    print(f"total {time.monotonic() - t:.0f}s")
    print("BUILD_OK", exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
