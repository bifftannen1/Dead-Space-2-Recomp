#!/usr/bin/env python3
r"""gen_hooks.py -- resolve the mods' hook names to guest addresses and write the dispatchers.

usage: python tools/modapi/gen_hooks.py --mods mods --names config/symbols_lane5_admitted.tsv
                                        --register app/generated/ds2_rex_diagnostic_register.cpp
                                        --out build/app/modapi_gen
       python tools/modapi/gen_hooks.py --selftest

Inputs
  mods/*.toml     [mod] name, hooks = [...], calls = [...], sources = [...] (paths relative
                  to mods/). `calls` are guest functions a mod calls (DS2_CALL).
  --names         PDB name table (va, size, name, mangled, sources), the same one the
                  diagnostics use. A hook is spelled as the table's `name`, its `mangled` name,
                  or `name@0xVA` to pick one of several functions that share a name.
  --register      the codegen function table: a hook address must be a registered function.

Rules (each failure prints GEN_HOOKS_FAIL and exits 1; nothing is written):
  * a name must resolve to exactly one address (a name is not a key: 0 or >1 is refused,
    with the candidates listed);
  * the address must be in the codegen function table;
  * every DS2_HOOK_*("...") in a mod's sources must be listed in that mod's manifest hooks,
    and every manifest hook must be used by a source or be marked for a DLL plugin
    (manifest `plugin`), so the manifest is the complete list of what the mod touches.

Outputs (written only when the content changes; a rerun reports 0 written):
  ds2_hook_table.h       constexpr name -> address table read by ds2::mod::hook_addr(), and
                         the name -> guest function table read by DS2_CALL
  ds2_hook_dispatch.cpp  one strong extern "C" sub_<VA> per hooked address -> Dispatch()
  mods.cmake             DS2_MOD_SOURCES + per-source DS2_MOD_ID, DS2_MOD_INPUTS
  gen_hooks.json         receipt: every hook, its address, the mods that use it
Prints GEN_HOOKS_OK <hooks> <written> <unchanged> on success.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import tempfile
import tomllib
from pathlib import Path

HOOK_RE = re.compile(r'DS2_HOOK_(BEFORE|AFTER|REPLACE)\s*\(\s*"([^"]+)"')
CALL_RE = re.compile(r'DS2_CALL\s*\(\s*\w+\s*,\s*"([^"]+)"')
# The symbol is whatever the table registers: sub_<VA> (codegen) or the PDB-derived identifier
# (tools/organize_generated.py --ids pdb, app/generated_named/ds2_rex_diagnostic_register.cpp).
REG_RE = re.compile(r"SetFunction\(0x([0-9A-Fa-f]+),\s*([A-Za-z_][A-Za-z0-9_]*)\)")


class Fail(Exception):
    pass


def load_names(path: Path):
    by_name: dict[str, set[int]] = {}
    by_mangled: dict[str, set[int]] = {}
    names_at: dict[int, set[str]] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            va = int(row["va"], 16)
            by_name.setdefault(row["name"], set()).add(va)
            m = row.get("mangled") or "-"
            if m != "-":
                by_mangled.setdefault(m, set()).add(va)
            names_at.setdefault(va, set()).add(row["name"])
    return by_name, by_mangled, names_at


def load_register(path: Path) -> dict[int, str]:
    """address -> registered symbol. A sub_<hex> symbol must carry its own address; any symbol
    registered twice, or an address registered twice, is refused (a name is not a key)."""
    txt = path.read_text(encoding="utf-8", errors="replace")
    out: dict[int, str] = {}
    for va, sym in REG_RE.findall(txt):
        if re.fullmatch(r"sub_[0-9A-Fa-f]+", sym) and int(sym[4:], 16) != int(va, 16):
            raise Fail(f"register table: {sym} registered at 0x{va}")
        if int(va, 16) in out:
            raise Fail(f"register table: 0x{va} registered twice")
        out[int(va, 16)] = sym
    if len(set(out.values())) != len(out):
        raise Fail("register table: a symbol is registered at two addresses")
    if not out:
        raise Fail(f"no SetFunction entries in {path}")
    return out


def resolve(spec: str, names) -> int:
    by_name, by_mangled, names_at = names
    m = re.fullmatch(r"(.+)@0x([0-9A-Fa-f]+)", spec)
    if m:
        name, va = m.group(1), int(m.group(2), 16)
        if name not in names_at.get(va, set()):
            raise Fail(f"hook '{spec}': no function named '{name}' at 0x{va:08X}")
        return va
    cands = by_name.get(spec, set()) | by_mangled.get(spec, set())
    if not cands:
        raise Fail(f"hook '{spec}': no such name in the PDB name table")
    if len(cands) > 1:
        lst = ", ".join(f"{spec}@0x{v:08X}" for v in sorted(cands))
        raise Fail(f"hook '{spec}' is ambiguous ({len(cands)} addresses): spell one of {lst}, or the mangled name")
    return next(iter(cands))


def c_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def generate(mods_dir: Path, names_path: Path, register_path: Path) -> tuple[dict[str, str], dict]:
    names = load_names(names_path)
    registered = load_register(register_path)
    manifests = sorted(mods_dir.glob("*.toml"))
    hooks: dict[str, dict] = {}  # spec -> {va, mods}
    calls: dict[str, dict] = {}
    sources: list[tuple[Path, str]] = []
    inputs: list[Path] = []
    seen_names = set()
    for mp in manifests:
        inputs.append(mp)
        try:
            t = tomllib.loads(mp.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise Fail(f"{mp}: {e}")
        mod = t.get("mod", {})
        name = mod.get("name")
        if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise Fail(f"{mp}: [mod] name missing or not [A-Za-z0-9_.-]+")
        if name in seen_names:
            raise Fail(f"{mp}: duplicate mod name {name}")
        seen_names.add(name)
        listed = list(mod.get("hooks", []))
        listed_calls = list(mod.get("calls", []))
        used: set[str] = set()
        used_calls: set[str] = set()
        for s in mod.get("sources", []):
            sp = (mods_dir / s).resolve()
            if not sp.is_file():
                raise Fail(f"{mp}: source {s} does not exist")
            inputs.append(sp)
            sources.append((sp, name))
            txt = sp.read_text(encoding="utf-8")
            used |= {h for _, h in HOOK_RE.findall(txt)}
            used_calls |= set(CALL_RE.findall(txt))
        missing = used - set(listed)
        if missing:
            raise Fail(f"{mp}: sources hook {sorted(missing)} but the manifest hooks do not list them")
        unused = set(listed) - used
        if unused and not mod.get("plugin"):
            raise Fail(f"{mp}: manifest hooks {sorted(unused)} are used by no source (and no plugin is named)")
        if used_calls - set(listed_calls):
            raise Fail(f"{mp}: sources DS2_CALL {sorted(used_calls - set(listed_calls))} but the manifest calls do not list them")
        for h in listed_calls:
            va = resolve(h, names)
            if va not in registered:
                raise Fail(f"call '{h}' -> 0x{va:08X} is not a function in the codegen table")
            calls.setdefault(h, {"va": va, "mods": []})["mods"].append(name)
        for h in listed:
            va = resolve(h, names)
            if va not in registered:
                raise Fail(f"hook '{h}' -> 0x{va:08X} is not a function in the codegen table")
            e = hooks.setdefault(h, {"va": va, "mods": []})
            e["mods"].append(name)
    by_va: dict[int, list[str]] = {}
    for h, e in hooks.items():
        by_va.setdefault(e["va"], []).append(h)

    tbl = ["// Generated by tools/modapi/gen_hooks.py - do not edit, do not commit.",
           "#pragma once", "#include <cstdint>", "#include <rex/ppc/context.h>", "namespace ds2::mod::gen {",
           "struct HookName { const char* name; uint32_t address; };",
           "inline constexpr HookName kHookTable[] = {"]
    for h in sorted(hooks):
        tbl.append(f"    {{{c_str(h)}, 0x{hooks[h]['va']:08X}u}},")
    if not hooks:
        tbl.append('    {"", 0u},  // no hooks')
    tbl += ["};", f"inline constexpr unsigned kHookCount = {len(hooks)};", "}  // namespace ds2::mod::gen", ""]
    call_vas = sorted({e["va"] for e in calls.values()})
    tbl += [f'extern "C" void {registered[va]}(PPCContext& __restrict ctx, uint8_t* base);' for va in call_vas]
    tbl += ["namespace ds2::mod::gen {", "struct CallName { const char* name; uint32_t address; void (*fn)(PPCContext&, uint8_t*); };",
            "inline constexpr CallName kCallTable[] = {"]
    for h in sorted(calls):
        tbl.append(f"    {{{c_str(h)}, 0x{calls[h]['va']:08X}u, &{registered[calls[h]['va']]}}},")
    if not calls:
        tbl.append('    {"", 0u, nullptr},  // no calls')
    tbl += ["};", "}  // namespace ds2::mod::gen", ""]

    dsp = ["// Generated by tools/modapi/gen_hooks.py - do not edit, do not commit.",
           "// One strong definition per hooked guest function. The generated code defines",
           "// <sym> as a weak alias of __imp__<sym>; this definition wins at link time. <sym> is the",
           "// symbol the --register table registers at the address (sub_<VA> or the PDB-derived name).",
           '#include "ds2_modapi.h"', ""]
    for va in sorted(by_va):
        sym = registered[va]
        label = by_va[va][0]
        # The slot is bound at static initialisation (before the loader's Finalize and before
        # any guest code runs), so Finalize can tell a dispatched address from one without.
        dsp += [f'extern "C" void __imp__{sym}(PPCContext& __restrict ctx, uint8_t* base);',
                f"static ::ds2::mod::detail::Slot* const ds2_slot_{sym} =",
                f"    ::ds2::mod::detail::BindSlot(0x{va:08X}u, {c_str(label)}, &__imp__{sym});",
                f'extern "C" void {sym}(PPCContext& __restrict ctx, uint8_t* base) {{',
                f"  ::ds2::mod::detail::Dispatch(*ds2_slot_{sym}, ctx, base);", "}", ""]

    def cm(p: Path) -> str:
        return p.as_posix()

    mk = ["# Generated by tools/modapi/gen_hooks.py - do not edit, do not commit.", "set(DS2_MOD_SOURCES"]
    mk += [f'    "{cm(p)}"' for p, _ in sources] + [")", "set(DS2_MOD_INPUTS"]
    mk += [f'    "{cm(p)}"' for p in inputs] + [")"]
    for p, n in sources:
        mk.append(f'set_source_files_properties("{cm(p)}" PROPERTIES COMPILE_DEFINITIONS "DS2_MOD_ID=\\"{n}\\"")')
    mk.append("")

    receipt = {"manifests": [m.name for m in manifests], "mods": sorted(seen_names),
               "hooks": {h: {"address": f"0x{e['va']:08X}", "mods": e["mods"]} for h, e in sorted(hooks.items())},
               "calls": {h: {"address": f"0x{e['va']:08X}", "mods": e["mods"]} for h, e in sorted(calls.items())},
               "dispatchers": len(by_va), "sources": [cm(p) for p, _ in sources],
               "names_table": cm(names_path), "register": cm(register_path), "registered_functions": len(registered)}
    files = {"ds2_hook_table.h": "\n".join(tbl), "ds2_hook_dispatch.cpp": "\n".join(dsp),
             "mods.cmake": "\n".join(mk), "gen_hooks.json": json.dumps(receipt, indent=1) + "\n"}
    return files, receipt


def write(out: Path, files: dict[str, str]) -> tuple[int, int]:
    out.mkdir(parents=True, exist_ok=True)
    w = u = 0
    for n, c in files.items():
        p = out / n
        if p.is_file() and p.read_text(encoding="utf-8") == c:
            u += 1
            continue
        p.write_text(c, encoding="utf-8", newline="\n")
        w += 1
    return w, u


def selftest() -> int:
    """Positive and negative controls on a synthetic name table, register and mods folder."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "names.tsv").write_text(
            "va\tsize\tname\tmangled\tsources\n"
            "0x82000000\t8\tA::unique\t?unique@A@@QAAXXZ\tt\n"
            "0x82000010\t8\tA::dup\t-\tt\n"
            "0x82000020\t8\tA::dup\t-\tt\n"
            "0x82000030\t8\tA::unregistered\t-\tt\n", encoding="utf-8")
        (d / "reg.cpp").write_text("".join(f"  registrar->SetFunction(0x{v:08X}, sub_{v:08X});\n"
                                           for v in (0x82000000, 0x82000010, 0x82000020)), encoding="utf-8")
        names, reg = d / "names.tsv", d / "reg.cpp"

        def case(hook: str, src_hook: str | None = None):
            m = d / "m"
            if m.exists():
                for p in m.iterdir():
                    p.unlink()
            m.mkdir(exist_ok=True)
            (m / "x.cpp").write_text(f'DS2_HOOK_AFTER("{src_hook or hook}", f);\n', encoding="utf-8")
            (m / "x.toml").write_text(f'[mod]\nname = "x"\nhooks = ["{hook}"]\nsources = ["x.cpp"]\n', encoding="utf-8")
            try:
                files, rc = generate(m, names, reg)
                return "ok", rc
            except Fail as e:
                return "fail", str(e)

        checks = [
            ("unique name resolves", case("A::unique"), "ok", "0x82000000"),
            ("mangled name resolves", case("?unique@A@@QAAXXZ"), "ok", "0x82000000"),
            ("name@va picks one of a duplicate", case("A::dup@0x82000020"), "ok", "0x82000020"),
            ("ambiguous name refused", case("A::dup"), "fail", "ambiguous (2 addresses)"),
            ("unknown name refused", case("A::nope"), "fail", "no such name"),
            ("wrong @va refused", case("A::dup@0x82000000"), "fail", "no function named"),
            ("unregistered function refused", case("A::unregistered"), "fail", "not a function in the codegen table"),
            ("source hook missing from manifest refused", case("A::unique", "A::dup@0x82000010"), "fail", "manifest hooks do not list"),
        ]
        bad = 0
        for label, (got, detail), want, needle in checks:
            ok = got == want and needle in (json.dumps(detail) if isinstance(detail, dict) else detail)
            bad += not ok
            print(("PASS " if ok else "FAIL ") + label + " -> " + got + ": " + (detail if isinstance(detail, str) else json.dumps(detail["hooks"])))
        # renamed register (organize --ids pdb): the dispatcher must define the registered name
        case("A::unique")
        (d / "reg2.cpp").write_text("  registrar->SetFunction(0x82000000, A__unique);\n"
                                    "  registrar->SetFunction(0x82000010, sub_82000010);\n", encoding="utf-8")
        f2, _ = generate(d / "m", names, d / "reg2.cpp")
        ren_ok = ('extern "C" void A__unique(' in f2["ds2_hook_dispatch.cpp"]
                  and "sub_82000000" not in f2["ds2_hook_dispatch.cpp"])
        (d / "reg3.cpp").write_text("  registrar->SetFunction(0x82000000, sub_82000010);\n", encoding="utf-8")
        try:
            generate(d / "m", names, d / "reg3.cpp")
            mis_ok = False
        except Fail:
            mis_ok = True
        for label, ok in (("renamed register symbol used by the dispatcher", ren_ok),
                          ("sub_ symbol at the wrong address refused", mis_ok)):
            bad += not ok
            print(("PASS " if ok else "FAIL ") + label)
        checks += [None, None]
        # idempotence control: second write of the same content writes 0 files
        case("A::unique")
        files, _ = generate(d / "m", names, reg)
        w1, _ = write(d / "out", files)
        w2, u2 = write(d / "out", files)
        idem = w1 == 4 and w2 == 0 and u2 == 4
        bad += not idem
        print(("PASS" if idem else "FAIL") + f" idempotent write: first {w1} written, second {w2} written / {u2} unchanged")
        print(("SELFTEST_OK" if not bad else "SELFTEST_FAIL") + f" {len(checks) + 1 - bad}/{len(checks) + 1}")
        return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mods")
    ap.add_argument("--names")
    ap.add_argument("--register")
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not (a.mods and a.names and a.register and a.out):
        ap.error("--mods, --names, --register and --out are required")
    try:
        files, rc = generate(Path(a.mods).resolve(), Path(a.names).resolve(), Path(a.register).resolve())
    except Fail as e:
        print("GEN_HOOKS_FAIL", e)
        return 1
    w, u = write(Path(a.out), files)
    for h, e in rc["hooks"].items():
        print(f"hook {e['address']} {h}  (mods: {', '.join(e['mods'])})")
    for h, e in rc["calls"].items():
        print(f"call {e['address']} {h}  (mods: {', '.join(e['mods'])})")
    print(f"GEN_HOOKS_OK {len(rc['hooks'])} hooks, {rc['dispatchers']} dispatchers, {len(rc['mods'])} mods; "
          f"{w} written, {u} unchanged -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
