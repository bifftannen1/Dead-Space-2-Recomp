# DS2Recomp-Source: Dead Space 2 (Xbox 360) PC recomp, as source

> **PRIVATE REPOSITORY. Do not make it public, fork it publicly, or redistribute it.**
> `src/` is game-derived: it is EA's and Microsoft's (XDK) copyrighted code, translated
> mechanically from the retail `default.xex` by ReXGlue, plus identifiers taken from the
> game's PDB. It is kept here for private study and building only.

This is the Dead Space 2 PC recomp from [DS2Recomp](https://github.com/bifftannen1/DS2Recomp)
packaged as a normal source project. The recompiled C++ under `src/` is the source of truth.
It is laid out in the game's original source folders with real PDB identifiers. The build
compiles it directly: it does not run `rexglue codegen` or the organizer.

Source snapshot: DS2Recomp `main` `fac9d8c` ("Named tree: real PDB identifiers instead of
sub_<ADDR>"), `app/generated_named/`, plus the codegen headers it includes.

## Layout
| Path | What |
|---|---|
| `src/DeadSpace/`, `src/EA/`, `src/EARS/`, `src/Havok/`, `src/XDK/`, `src/BFX/` | Recompiled functions, one file per original source file (PDB paths) |
| `src/_units/` | 157 build units. Each `#include`s the original-path files that make up one codegen TU, which keeps the objects identical to codegen's. |
| `src/ds2_rex_diagnostic_{funcs,funcs.N,pch,init}.h`, `*_init.cpp`, `*_register.cpp` | Declarations, PCH, image config and the function registration table |
| `src/ds2_names.h`, `src/symbols.tsv`, `src/manifest.tsv` | PDB name map (`symbols.tsv`: address, codegen name, PDB name) and per-function placement |
| `src/app/` | Host app: `main.cpp`, `main_mods.cpp`, `ds2_app.h` (a `rex::ReXApp`) |
| `src/modapi/`, `mods/` | Mod API headers and loader, plus example mods (`fov`, `intro_skip`, `loose_demo`) |
| `CMakeLists.txt`, `cmake/ds2_recomp.cmake` | Builds `ds2.exe` from `src/` against the installed ReXGlue SDK |
| `sdk/` | `REXGLUE_BASE.txt` (pinned upstream commit + patch sha256), `rexglue_ds2.patch`, `PROVENANCE.txt`, `upstream/` notes |
| `tools/build.py` | Fetches, patches and builds the SDK, then builds the app and stages your XEX |
| `tools/extract_xex.py` | Copies `default.xex` out of your disc image (read-only) |
| `tools/smoke_test.py` | Launches the game, captures frames with PrintWindow (no focus steal), closes it |
| `tools/diagnostics/compare_objs_exact.py` | Byte-exact A/B of guest objects between two builds |
| `tools/modapi/` | Mod API generators (`gen_hooks.py`, `gen_types.py`) and the PDB name table they read |
| `docs/` | `BUILDING.md`, `MODDING.md`, `NAMED_TREE.md`, `REGENERATING.md` |

## Quick start
```
python tools\build.py --iso "D:\isos\Dead Space 2 (Disc 1).iso"
build\bin\ds2.exe
```
See [docs/BUILDING.md](docs/BUILDING.md). You need your own retail disc: the XEX must match
sha256 `928597dd...3408`.

## Regenerating `src/`
This repo has no codegen pipeline. To regenerate the tree from a XEX (codegen manifest,
partition seed, allocation tables, organizer, naming chain), use DS2Recomp. See
[docs/REGENERATING.md](docs/REGENERATING.md).
