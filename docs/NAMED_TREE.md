# Named tree

`tools/build.py` runs `tools/organize_generated.py --verify` after codegen. The organizer writes `app/generated_named/`: the same 47,419 function bodies filed under the game's original source paths. By default (`--ids pdb`), every named function is called by its real PDB name instead of ReXGlue's `sub_<ADDR>`. The app compiles that tree. Registration by address, the machine code and the PPC ABI are unchanged. The tree is game-derived and gitignored.

```
python tools/organize_generated.py --verify            # (re)write + verify; a second run changes 0 files
python tools/organize_generated.py --verify --ids sub  # previous behaviour: sub_ symbols + #define aliases
python tools/build.py --xex default.xex [--flat]       # --flat compiles app/generated as before
```

## Identifiers (`--ids pdb`, the default)
How ReXGlue names a function: `DEFINE_REX_FUNC(sub_X)` defines `__imp__sub_X` and a weak alias `sub_X` (`app/generated/ds2_rex_diagnostic_pch.h`; `REX_FUNC`/`REX_EXTERN` in the SDK's `rex/ppc/func.h` give the signature and `extern "C"`). `DECLARE_REX_FUNC(sub_X)` declares both. Calls are `sub_X(ctx, base);`. `ds2_rex_diagnostic_register.cpp` (`SetFunction(0xADDR, sub_X)`) and `ds2_rex_diagnostic_init.cpp` (`PPCFuncMappings`) bind address to symbol. No macro or tool derives the address from the identifier; the runtime resolves by address only. Mods override the weak `sub_X` with a strong definition (`tools/modapi/gen_hooks.py`).

The organizer rewrites each named function's identifier to a PDB-derived one. The rewrite covers both the `sub_X` and `__imp__sub_X` tokens, everywhere in the tree:
- definitions and calls;
- the per-TU declaration headers: `_units/ds2_rex_diagnostic_funcs.N.h`, which the unit's quoted `#include` finds before `app/generated`'s copy;
- the full surface, `ds2_rex_diagnostic_funcs.h` and `ds2_rex_diagnostic_init.h`;
- the registration and mapping tables, `ds2_rex_diagnostic_register.cpp` and `ds2_rex_diagnostic_init.cpp`. These are now compiled from the named tree (`sources.cmake`).

`gen_hooks.py` takes each hooked function's symbol from the registration table. With `DS2_NAMED_TREE=ON`, `app/modapi/modapi.cmake` passes it the named tree's table, so the dispatchers override the renamed weak symbol.

The tree has no `#define` aliases any more. `ds2_names.h` lists the map as comments, and `symbols.tsv` (`addr, codegen_symbol, symbol`) is the machine-readable map.

**Scheme:** the identifier is the PDB qualified name, with `::` written as `__` and every other non-identifier character as `_`. This is the allocation/names `ident` without its `_<ADDR>` suffix, e.g. `hkpConstraintData::getConstraintInfoUtil` becomes `hkpConstraintData__getConstraintInfoUtil`. A parent+offset piece keeps its offset: `hkpConstraintData__addInstance_0x18`. The suffix `_<ADDR>` stays on in three cases:
1. **Free functions** (no `::` in the PDB name). They are `extern "C"` in the same link as the host CRT, Win32, SDL and the SDK, so a bare `memcpy`, `XamUserGetName` or `speex_bits_pack` could bind to, or override, a host symbol.
2. **Collisions:** the stripped name is shared by several functions (overloads, folded copies, pieces). Every member of the group keeps its address.
3. **Clashes:** the stripped name, or its `__imp__` form, equals another codegen symbol (imports, `__savegprlr_*`, ...).

The result must be a unique C++ identifier, must never be `sub_<8 hex>` and must not start with `__imp__`; anything else is a refusal. Folded functions use their primary (first sorted) name, with the other names in the `folded aliases` comment. Unnamed functions keep `sub_<ADDR>`. `loc_` labels are untouched.

Counts (codegen 2026-10-08 tables): 46,094 of 47,419 defined functions renamed. 35,502 have a bare name; 5,061 keep `_<ADDR>` because of a collision (1,782 groups); 5,531 are free functions that keep `_<ADDR>`; 0 clash with a codegen symbol. The other 1,325 keep `sub_<ADDR>`: they are unnamed, or their codegen symbol was never `sub_`.

Before (`--ids sub`) and after (`--ids pdb`), `Havok/source/physics/dynamics/constraint/hkpconstraintdata.cpp`:
```cpp
// before
#define hkpConstraintData__addInstance_822E8CD8 sub_822E8CD8
DEFINE_REX_FUNC(sub_822E8CD8) {
	...
	// b 0x8232b648
	sub_8232B648(ctx, base);

// after
//   codegen symbol: sub_822E8CD8 (registered by address 0x822E8CD8)
DEFINE_REX_FUNC(hkpConstraintData__addInstance) {
	...
	// b 0x8232b648
	hkString__memSet(ctx, base);
```
and in `ds2_rex_diagnostic_register.cpp`, `SetFunction(0x822E8CD8, sub_822E8CD8)` becomes `SetFunction(0x822E8CD8, hkpConstraintData__addInstance)`.

**Gates for the rename** (on top of those below):
- **`--verify`:**
  - every body equals the codegen body through the map, and comes back byte-identical through the inverse map applied to every identifier token (47,419/47,419);
  - the 161 renamed support files (4 tables/headers + 157 unit headers) pass the same two checks;
  - 0 renamed `sub_` tokens are left on non-comment lines;
  - the registration table registers the same 47,695 addresses in the same order;
  - each named TU's standalone header declares exactly what it calls (4,235/4,235; on the first draft's tree, where this was a bug, the check measured 556/4,235);
  - in-memory controls: a missed rename and a swapped call target are both detected;
  - `--ids sub` reproduces the previous tree byte for byte.
- **`compare_objs_exact.py --name-map app/generated_named/symbols.tsv`:** the symbol names in B's symbol table and relocation targets are mapped back to codegen's before hashing. Raw section bytes are never touched. `--support-a/--support-b` also pair the init/register objects. Controls: the raw names must differ (the map is doing the work), and a map with two names swapped must be detected.

## Layout (functions per top-level directory; files)
| dir | origin | functions | files |
|---|---|---|---|
| DeadSpace/ | `c:\builds\packages\deadspace\…` (game code) | 15,869 | 1,230 |
| EARS/ | `ears_*` engine packages | 12,396 | 844 |
| EA/ | other EA packages (eathread, eastl, BlazeSDK, rwaudiocore, …) | 7,716 | 743 |
| Havok/ | Havok 6.5 source tree | 5,681 | 729 |
| XDK/ | Xbox 360 XDK libraries, CRT, `XDK/imports/<dll>` import thunks | 3,824 | 596 |
| BFX/ | `c:\bfx_releases\3.0` | 1,664 | 78 |
| `_units/` | 157 compile units (below) | 203 UNDETERMINABLE kept inline | 157 |

Each file is `<original path>` with `.cpp` appended unless it already ends in `.cpp` (`ldo.c` becomes `ldo.c.cpp`, a header becomes `x.h.cpp`). `config/source_roots.tsv` maps the build-machine roots.

Example (`config/allocation.tsv.gz`):

| address | class | name | file |
|---|---|---|---|
| 0x821D0000 | PDB_EXACT | `EA::Thread::DynamicThreadArray::AddDynamicThreadHandle` | `EA/eathread/source/pc/eathread_pc.cpp` |
| 0x822E8D00 | PDB_EXACT | `hkpConstraintData::getConstraintInfoUtil` | `Havok/source/physics/dynamics/constraint/hkpconstraintdata.cpp` |
| 0x822BDE10 | PDB_DATA_OWNER | — | `DeadSpace/src/core/graphics/shaderwrappers/sm_deadspace_holo.cpp` |
| 0x824C5E28 | MANUAL_EVIDENCE | — | `EARS/ears_audio/src/asoundmanager.cpp` |

## Why compile units, and the exactness proof
Splitting the code into different translation units changed clang's output for 22 functions.
- The IR going into the SLP vectoriser was identical in both builds; its output was not.
- The codegen for these functions depends on the other functions in the same TU.

So the build compiles `_units/ds2_rex_diagnostic_unit.N.cpp`, one per codegen TU N. Each one starts with TU N's declaration header and `#include`s the named files in codegen's exact order. A per-block selector (`DS2_UNIT`, `DS2_LO`, `DS2_HI`) picks the functions. clang sees the same translation units; only debug line info changes, and it now points at the named files.

Verification:
- **`--verify`:** 47,419/47,419 functions exactly once; bodies byte-identical; 157/157 units resolve to their codegen TU in order; a second run changes 0 files. Dropping one function from a unit is detected (157 → 156).
- **`tools/diagnostics/compare_objs_exact.py`:** object N of the flat build and of the named build are compared whole. Every non-debug section's bytes (`.text`, `.rdata` incl. jump tables, `.pdata`, `.xdata`), every relocation and the symbol table are compared. Result: 157/157 identical. Controls: a byte copy compares equal; one flipped `.text` byte is detected.
- **Smoke test:** `tools/smoke_test.py` gives SMOKE_RENDERED for the named build.

## Allocation: how every function got its file
Every class's precision and controls are in `config/allocation_schema.md` and `docs/allocation_receipt.json`. Counts out of 47,695:

| class | count |
|---|---|
| PDB_EXACT | 30,867 |
| LINK_BRACKET | 12,972 |
| INFERRED | 1,494 |
| PDB_DELTA | 800 |
| PDB_BYTES | 690 |
| LINK_RANGE | 339 |
| FOLDED | 196 |
| PDB_DATA_OWNER | 64 |
| MANUAL_EVIDENCE | 4 |
| NAME_MODULE | 66 |
| UNDETERMINABLE | 203 |

Names: 30,880 functions carry their PDB name, 345 a byte-matched PDB name and 650 a parent+offset name. 137 have several folded names, none admitted. The other 15,683 have a file but no name.

## Naming pass (`tools/naming/name_pass.py` -> `config/names.tsv.gz`)
Gives a PDB procedure name to file-only functions. The organizer reads the table (`--names`), sets the comment name and the `#define` alias. Symbols and registration by address are unchanged. A row for an already-named address, an unknown address or a non-identifier is a refusal. Named: 31,875 -> 43,573 of 47,695 (the other 4,122 are listed under Residue).

| method | rule | LOO (hidden named functions) | control | admitted |
|---|---|---|---|---|
| N1 offset start | retail x -> proto x-d (bracket delta, or one side's delta if exactly one passes); a proc starts there in the same obj, unclaimed, one distinct name, size within tolerance, opcode shape >= 0.75, no contradicting named callee, and no other proc within +-2 in the obj passes the same checks | 29,646/29,646 (bytes differ 23,923/23,923; one-sided 1,883/1,883) | next-proc shift 52/30,806 = 0.17% | 6,707 |
| N2 ordered alignment | between two named functions of one obj, unnamed retail functions and unclaimed procs are equal in number and pass the N1 checks pairwise in order | hidden blocks of 1-8: 11,164/11,164 | shifted proc window 0/17,692 | 3,376 |
| N3 callee multiset | >= 2 direct callees, all named; the callee-name multiset matches exactly one unclaimed proc of the obj | 804/804 | shuffled obj 0/1,500 | 12 |
| N4 refs/strings tie-break | for the sibling ambiguity: among unclaimed same-obj procs within +-2 of the N1 target that fit the size, the one N1-passing proc whose formed data addresses (mapped through the evidence map) and string texts plus named callees agree (>= 2) with no contradiction, while every other candidate contradicts | 75/75 of 144 sibling-ambiguous hidden functions (min agree 2 was chosen after min agree 1 measured 92/93); held-out, rule frozen (`--heldout-n4`, seed 20261007, the 144 excluded, `docs/names_n4_heldout.tsv`): near-shape +-2 candidates 858/858 of 1,289 (Wilson 95% lower 0.9955); claims lifted 17,232/17,234 of 30,384 (lower 0.9996) | another function's refs: 0/144 | 938 |
| N5 local re-alignment | each side's nearest named obj-mate carries its own delta; the target passes N1's checks with no passing sibling | 29,899/29,899; where the global bracket misses the start 37/37 | next-proc shift 1/30,806 | 46 |
| N6 folded names | the N1 target is a proc start carrying several names (one body: identical-code folding in the prototype). The checks pass, the obj is among the procs' objs and no proc start within +-2 passes. The primary name is the first sorted name. All names are recorded in `aliases` (the claim) and in a `folded aliases` comment | AMBIGUOUS-start functions, hidden: 92/92 of 137; true name in the alias list (unique truth) 30,260/30,283 | next-proc shift 0/137 | 389 |
| N7 inexact match | candidates are the single-name, unclaimed procs of the obj within +-3 of the N1 target. Take the best normalised opcode-token similarity (difflib) with no contradicting callee. Accept at >= 0.95 with a margin of >= 0.05 over the runner-up, and with >= 1 agreeing data ref/string or callee and no contradiction. The thresholds were tuned on a crc32 split, then frozen. The control criterion and the ref dimension were added after a first freeze measured held-out control 301/8,000 | held-out 5,907/5,907 (Wilson 95% lower 0.99935) | window moved off the truth 31/8,000 = 0.39%. Untouched partition (salt `N7FRESH`), rule frozen at 173b56c: 5,442/5,444 (Wilson lower 0.99866), control 25/7,356 | 93 |
| N8 q/d tie-break (refused) | among the N4 ambiguity candidates, the one whose name's unique q/d proc body matches retail best | tuning best 148/152 (Wilson lower 0.934), so no setting reaches 0.99 and nothing is admitted | - | 0 |
| N9 folded starts | the allocation's 137 AMBIGUOUS starts (evidence-map start carrying several folded names): primary = first sorted name, every name in `aliases` (the claim). The organizer accepts N9 rows for AMBIGUOUS status only, and only with an alias list | an alias corroborated by a unique q/d proc of the same body shape: 130/137; the same rule on unique truth gives the true name in the alias list 30,260/30,283 | - | 137 |

Each run also checks the table (each proc start has exactly that one name, in the row's obj; no proc or address twice). A copy with one row moved to the neighbouring proc and a copy with a duplicated row must both fail the check. Residue (4,122): proto target is not a proc start (1,890); sibling ambiguity (1,063); target claimed (380); UNDETERMINABLE (269); folded names with a passing sibling (217); size (200); other (103). `docs/names_receipt.json` holds the numbers. A second run writes 0 files.

## UNDETERMINABLE (269)
No measured method places these functions, and none is given a folder. Each stays verbatim in its codegen unit file under its codegen TU. `config/undeterminable.tsv` lists, per function, the candidate owner objs with source files, the evidence (strings, data refs, callees, callers) and every method that refused it with that method's measured precision.

Per-function audit (`tools/naming/residue_audit.py`):

| audit class | count | meaning |
|---|---|---|
| c | 167 | No evidence at all: no strings, data refs or allocated callees. |
| b | 73 | Evidence points to ≥ 2 candidates or outside the candidate list. |
| a | 29 | Single-candidate evidence (callee-only or adjustor-thunk target). The rule measures 0.908–0.924 on known functions, and hand review refused each one (`config/residue_manual.tsv`). |

- **Patterns:** 129 ordinary code, 108 `lis; addi|lwz; blr` address/global returns, 14 tail-branch stubs, 12 this-adjustor thunks, 6 indirect or vcall stubs.
- **No prototype counterpart:** 28 have no byte counterpart in any of the f, q or d PDBs (retail-only code).

Methods measured and refused (receipt):

| method | measured | gate |
|---|---|---|
| Call-graph tie-break | 0.925 | ≥ 0.99 |
| FOLDED_TRACED (fold + callers) | 0.843; shuffled-caller control 15% | ≥ 0.99, control < 1% |
| ICF leader rule (earliest copy) | 0.342; random 0.386 | ≥ 0.99 |
| LAYOUT_RUN | 0.833 | ≥ 0.99 |
| DATA_REFS (all refs) | 0.513 | ≥ 0.99 |
| q/d PDB byte match | 0.973 / 0.946 | ≥ 0.99 |
| Retail-only folder by neighbours | ≤ 0.974 | ≥ 0.99 |
| Padding at obj boundaries | not distinguishable (≈ 5% everywhere) | — |
| .xdata runs | not applicable (Xenon `.pdata` is packed, with no xdata pointer) | — |

## Rebuilding the allocation (needs the local PDBs and evidence tables)
```
python tools/naming/pdb_procs.py --pe ds2_f.pe --pdb deadspace_f.pdb --out build/naming/pdb_procs.tsv.gz   # also q, d -> pdb_procs_q/_d
python tools/naming/build_allocation.py --generated app/generated --procs build/naming/pdb_procs.tsv.gz
python tools/naming/residue_audit.py && python tools/naming/write_residue_manual.py && python tools/naming/write_undeterminable.py
python tools/naming/build_allocation.py --generated app/generated --procs build/naming/pdb_procs.tsv.gz   # gate passes; 2nd run: 0 changed
```

## Names after the residue stages (2026-10-07)
`config/names.tsv.gz` is written by `tools/pdbchain` (`python tools/pdbchain --config tools/pdbchain/examples/ds2.toml`, then copied to `config/`):
- 14,703 rows: N1-N9 11,698, plus the residue stages T6 503, T2 1,010, T1 763, T4 74, T3R 473, T3S 23, T5 57, T7 83 and MANUAL_EVIDENCE 19.
- 46,578 of 47,695 functions are named. 1,117 are unnamed, 118 of which are TAIL_OF rows: lone `blr` pseudo-functions that ReXGlue split off the previous procedure.
- `NAME_MODULE` (66) is an allocation from the name's PDB module, for rows that were UNDETERMINABLE and that an obj-free stage named (stage A8).
- Stage gates, precision and controls: `tools/pdbchain/README.md` and `docs/pdbchain_receipt.json`.
