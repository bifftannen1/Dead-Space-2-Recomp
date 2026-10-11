![Dead Space 2](docs/img/header.jpg)

# Dead Space 2 Recomp

> No game data is included. You need your own retail copy of Dead Space 2 to build and play.

A static recompilation of the Xbox 360 version of Dead Space 2 that runs natively on PC.
The whole game executable (47,419 functions) is translated to C++ by
[ReXGlue](https://github.com/rexglue/rexglue-sdk) and laid out using the game's PDB:
original source folders, real function names, original file boundaries.

Dead Space 2 already has an official PC release; this project isn't a replacement for it.
It has two goals:
- **Portability:** turn the 360 version into plain C++ that can move to ARM devices (Switch and others).
- **PDB mapping:** test how far a prototype PDB can name and place recompiled code directly,
  with no decompilation step, with every name and source file backed by measured precision.

Mods are supported through a hook API keyed by those PDB names.

## Status
| | Base (retail) | TU2 (Title Update #2) |
|---|---|---|
| Functions | 47,419 | 47,739 |
| Named with their PDB name | 46,316 (97.7%) | 46,105 (96.6%) |
| Placed in their original source file | 47,171 (99.5%) | 47,429 (99.4%) |
| Boots and plays | yes | yes (smoke-tested; the code is carried from base) |

- **TU2 names:** carried from base by proof. 45,912 are *proven* and 885 *probable*. The probable ones need an opt-in before a mod can hook them.
- **Disc 2:** with Title Update #2 it patches to an image byte-identical to Disc 1, so the TU2 build runs it too. Switching discs mid-game isn't supported yet ([TODO](docs/TODO.md)).
- **DLC, *Dead Space 2: Severed*:** working, but **not fully playtested**. The package installs with `tools/extract_dlc.py` and the game mounts it. Severed has been smoke-tested, not played through.
- **Multiplayer and Xbox Live features** (online play, leaderboards, achievements syncing, marketplace) are **not tested yet**.
- **Launcher:** coming soon. It will be a single `DS2Launcher.exe` that takes your discs, the title update and the DLC, checks them and starts the game.

## Sources
| | |
|---|---|
| Retail Disc 1 | *Dead Space 2 (World) (En,Fr,Es)*, Disc 1 `default.xex`, Title ID `454108DF`, Media ID `2915C8F8` |
| Retail Disc 2 | Same release, Disc 2 `default.xex`, Media ID `403243EC` |
| Title Update | *Dead Space 2 Title Update #2* (`TU_12K226V_000000C000000.00000000000G3`); `disc001/default.xexp` sha256 `8d386d37…01ec2`, which patches the XEX to version `0.0.2.3` |
| DLC | *Dead Space 2: Severed* (content type `00000002`, package `1B7ECDF8…7E45`) |
| PDB | `deadspace_f.pdb`, 2010-11-19 prototype build |

The prototype is a near-exact match for retail: of 22,976 functions compared by compiled size,
22,962 (99.94%) are identical. The 14 that differ are all XDK runtime code (CRT file I/O, heap);
the game's own code matches throughout.

## Quick start
You need your own Dead Space 2 Disc 1 image (the XEX must match sha256 `928597dd…3408`).

**Recommended: build with Title Update #2** (your own `disc001\default.xexp`):
```
python tools\build.py --target tu2 --iso "D:\isos\Dead Space 2 (Disc 1).iso" --xexp "D:\tu2\disc001\default.xexp"
build\bin\ds2.exe
```
Without the title update (the retail build):
```
python tools\build.py --iso "D:\isos\Dead Space 2 (Disc 1).iso"
build\bin\ds2.exe
```
DLC: `python tools\extract_dlc.py <package> --user-root <folder>`, then `ds2.exe --user_data_root=<folder>`.

Requirements and options: [docs/BUILDING.md](docs/BUILDING.md).

## Layout
| Path | What |
|---|---|
| `src/DeadSpace`, `EA`, `EARS`, `Havok`, `XDK`, `BFX` | Recompiled game code, one file per original source file |
| `src_tu2/` | The same tree for Title Update #2 (`--target tu2`); `src/app` and `src/modapi` are shared |
| `src/_units/` | 157 build units matching the codegen translation units |
| `src/symbols.tsv`, `src/ds2_names.h` | Address → PDB name map |
| `src/app/` | Host app (`ds2.exe`) |
| `src/modapi/`, `mods/` | Mod API and example mods |
| `sdk/` | Pinned ReXGlue commit and the DS2 patch |
| `tools/` | Build, XEX extraction, smoke test, object comparison, mod generators, `pdbchain` (PDB matching, including `V0_PREV_BUILD` for carrying names to a new build), `tu2/` (build-to-build tooling) |

## Docs
- [BUILDING.md](docs/BUILDING.md): build, run, smoke test
- [MODDING.md](docs/MODDING.md): hooks by PDB name, loose files, PDB struct headers
- [NAMED_TREE.md](docs/NAMED_TREE.md): how functions got their names and files
- [PDB_MATCHING.md](docs/PDB_MATCHING.md): matching tools, method and timeline
- [REGENERATING.md](docs/REGENERATING.md): refreshing `src/` from the pipeline repo
- [TODO.md](docs/TODO.md): roadmap (raw mouse, ultrawide, above 60 FPS, graphics options, multiplayer, disc swap)
