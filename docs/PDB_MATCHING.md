# PDB matching

How the retail functions got their names and source files from the prototype PDB.

## Tools
| Tool | Role |
|---|---|
| [`tools/pdbchain/`](../tools/pdbchain/README.md) | Maps every recompiled retail function to its prototype object file, source file and PDB name. Title-agnostic; each stage is gated on measured precision. |
| `tools/pdbchain/stages/prevbuild.py` | Stage `V0_PREV_BUILD`: carries a previous build's names and files to a new build (for example a title update) by proof, before the PDB stages run. Proven tier: Wilson lower bound ≥ 0.99; probable tier: precision ≥ 0.99 and Wilson ≥ 0.95 (not hookable by mods by default) |
| `tools/tu2/` | Build-to-build tooling: ground-truth audit (`gt.py`), pointer-seed scan and pruner, PE assembly from an image dump, tree diff |
| `tools/pdbreader/` | PDB (MSF/DBI/TPI) and PE reader that pdbchain uses (`identity_donor.py`, `identity_image.py`) |
| `tools/deswitch/` | Source-path normalisation and tree helpers that pdbchain's path rules mirror |
| `tools/organize_generated.py` | Writes the codegen output into the named tree and verifies it byte for byte (expects the DS2Recomp layout: `app/generated`, `config/`) |
| `tools/diagnostics/compare_objs_exact.py` | Proves the named build's objects equal the flat codegen build's |
| `tools/modapi/gen_hooks.py`, `gen_types.py` | Turn the name table and PDB types into the mod API |
| `llvm-pdbutil` | Independent PDB reader used to cross-check results |

The helpers are pinned by sha256 in the config. The example configs in
`tools/pdbchain/examples/` use placeholders (`<repo>`, `<pdb_dir>`, `<work_dir>`) to fill in.
`deswitch_srctree.py` needs `pip install itanium_demangler`.

## Inputs
- Retail Disc 1 `default.xex` (Title ID `454108DF`), extracted to its PE
- Prototype `deadspace_f` PE + PDB (2010-11-19, primary), plus `_q` and `_d` for cross-checks
- The recompiler's registered function list (ReXGlue `*_register.cpp`)

## Method
1. **Evidence map:** anchor retail functions with exact byte/digest matches to the prototype.
2. **Allocation:** place every function in an object file and source file (exact PDB match,
   link-order brackets, data ownership).
3. **Naming (N1–N9):** name the rest by offset alignment, callee and data-reference matching,
   inexact opcode similarity and folded-code handling.
4. **Residue stages (T1–T9):** RTTI vtable slots, strict constants and other tie-breaks.

Every stage is admitted only at a Wilson 95% lower bound ≥ 0.99 on hidden known answers, with a
control and a held-out split. A second run must change 0 files.

## Timeline
| Date | Milestone | Named functions |
|---|---|---|
| 2026-10-06 | ReXGlue codegen fixes, PE/PDB evidence map | |
| 2026-10-07 | Named source tree; every function allocated to a file | 31,875 |
| 2026-10-07 | Naming passes N1–N9 | 43,573 |
| 2026-10-07 | pdbchain built; DS2 regression byte-identical; residue stages | 46,578 |
| 2026-10-08 | ≥ 0.99 bar on every stage; T8/T9 added; 203 functions left unplaceable | 46,578 |
| 2026-10-08 | Real PDB identifiers replace `sub_<ADDR>` in the source tree | |
| 2026-10-10 | Public source release | |
| 2026-10-10 | Title Update #2 build: names carried across the relink by proof (98.9% kept order) | |
| 2026-10-11 | `V0_PREV_BUILD` stage with proven/probable tiers; regression byte-identical to the original chain | 46,797 carried to TU2 |

Result: 46,578 of 47,695 functions named; see [NAMED_TREE.md](NAMED_TREE.md).
