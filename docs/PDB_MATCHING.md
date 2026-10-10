# PDB matching

How the retail functions got their names and source files from the prototype PDB.

## Tools
| Tool | Role |
|---|---|
| [`tools/pdbchain/`](../tools/pdbchain/README.md) | Maps every recompiled retail function to its prototype object file, source file and PDB name. Title-agnostic; each stage is gated on measured precision. |
| `organize_generated.py` (DS2Recomp) | Writes the codegen output into the named tree and verifies it byte for byte |
| `tools/diagnostics/compare_objs_exact.py` | Proves the named build's objects equal the flat codegen build's |
| `tools/modapi/gen_hooks.py`, `gen_types.py` | Turn the name table and PDB types into the mod API |
| `llvm-pdbutil` | Independent PDB reader used to cross-check results |

pdbchain also needs an external PDB/PE reader (`identity_donor.py`, `identity_image.py`), pinned
by sha256 in the config. The example configs in `tools/pdbchain/examples/` use placeholder
paths (`<pdb_dir>`, `<work_dir>`, …) to fill in.

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

Result: 46,578 of 47,695 functions named; see [NAMED_TREE.md](NAMED_TREE.md).
