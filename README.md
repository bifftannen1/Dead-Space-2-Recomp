# Dead Space 2 Recomp

> No game data is included. You need your own retail copy of Dead Space 2 to build and play.

A static recompilation of the Xbox 360 version of Dead Space 2 that runs natively on PC.
The whole game executable (47,419 functions) is translated to C++ by
[ReXGlue](https://github.com/rexglue/rexglue-sdk) and laid out using the game's PDB:
original source folders, real function names, original file boundaries.

Built with future ARM ports (Switch) and mods in mind.

## Sources
| | |
|---|---|
| Retail | *Dead Space 2 (World) (En,Fr,Es)*, Disc 1 `default.xex`, Title ID `454108DF`, Media ID `2915C8F8` |
| PDB | `deadspace_f.pdb`, 2010-11-19 prototype build |

The prototype is a near-exact match for retail: of 22,976 functions compared by compiled size,
22,962 (99.94%) are identical. The 14 that differ are all XDK runtime code (CRT file I/O, heap);
the game's own code matches throughout.

## Quick start
You need your own Dead Space 2 Disc 1 image (the XEX must match sha256 `928597dd…3408`).
```
python tools\build.py --iso "D:\isos\Dead Space 2 (Disc 1).iso"
build\bin\ds2.exe
```
Requirements and options: [docs/BUILDING.md](docs/BUILDING.md).

## Layout
| Path | What |
|---|---|
| `src/DeadSpace`, `EA`, `EARS`, `Havok`, `XDK`, `BFX` | Recompiled game code, one file per original source file |
| `src/_units/` | 157 build units matching the codegen translation units |
| `src/symbols.tsv`, `src/ds2_names.h` | Address → PDB name map |
| `src/app/` | Host app (`ds2.exe`) |
| `src/modapi/`, `mods/` | Mod API and example mods |
| `sdk/` | Pinned ReXGlue commit and the DS2 patch |
| `tools/` | Build, XEX extraction, smoke test, object comparison, mod generators |

## Docs
- [BUILDING.md](docs/BUILDING.md): build, run, smoke test
- [MODDING.md](docs/MODDING.md): hooks by PDB name, loose files, PDB struct headers
- [NAMED_TREE.md](docs/NAMED_TREE.md): how functions got their names and files
- [REGENERATING.md](docs/REGENERATING.md): refreshing `src/` from the pipeline repo
