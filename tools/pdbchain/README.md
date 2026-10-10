# pdbchain

A title-agnostic tool for Xbox 360 static recomps that have a prototype PDB. It maps every function the recompiler registered in the retail image to its original object file, source file and PDB name. Every method it chains was measured on Dead Space 2. A method admits results only when its leave-one-out gate on hidden known answers passes, together with a control and a held-out split.

## Quick start
1. Extract the retail basefile PE from the XEX (decrypted, decompressed); have each prototype PE + PDB pair.
2. Copy `examples/ds2.toml` and point `inputs`, `[[prototype]]`, `registered` and `pdb.reader_dir` at your files.
3. Run the chain, then run it again: the second run must print `files changed: 0`.

```
python tools/pdbchain --config my_title.toml            # full chain
python tools/pdbchain --config my_title.toml --dry      # measure and gate, write nothing
python tools/pdbchain --config my_title.toml --only evidence|allocation
python tools/pdbchain/regress_ds2.py                    # DS2 regression (needs the local DS2 inputs)
python tools/pdbchain/probe_pdb_tables.py               # PDB-table extraction vs build/naming (f/q/d)
python tools/pdbchain --config my_title.toml --dev-cache # development: reuse allocation / N1-N9 when code+inputs unchanged (regress refuses such a receipt)
```

Exit 0 with `PDBCHAIN DONE` means three things: every admitted stage passed its gate, the tables passed their invariants, and the self-shakes caught their perturbations. Any refusal exits 2 and writes nothing. A second run with the same inputs prints `files changed: 0`.

## Inputs
| config key | what |
|---|---|
| `inputs.retail_pe` | Retail basefile PE, extracted from the XEX (decrypted and decompressed). The chain reads PE32 in mapped layout. |
| `[[prototype]]` `role="primary"` | The prototype PE + PDB that is address-mapped to retail (DS2: `f`, the build closest to retail). |
| `[[prototype]]` `role="aux"` | Other prototype PE + PDB pairs. They are used for the counterpart census (residue reasons) and by the opt-in stages `AUX_BYTES` and `N8`. |
| `inputs.anchors` | TSV with `name` and `va` columns: retail functions already identified (DS2: 23,921 digest matches). Seeds the evidence map. |
| `registered` | The recompiler's function list. `adapter="rexglue"`, `path=<codegen dir>`: reads `*_register.cpp` and the `*_recomp.*.cpp` call graph. `adapter="tsv"`, `path=<addr[,symbol]>`: optional `calls` TSV (caller, callee); without it, calls are decoded from `bl` targets. |
| `inputs.evidence_map` | Optional. Reuse an existing E1 map instead of building one. |
| `inputs.keyed_feed` | Optional. Keyed `.pdata`/start rows (`kind`, `proto_addr`, `retail_addr`) from the R1-13b v2 keyed methods (imports by library+ordinal, RTTI chains, `.pdata` begin+size). |
| `inputs.mangled_names` | Optional. `va`, `mangled` (adds signatures). `inputs.undname`: MSVC `undname.exe` (prototypes). |
| `inputs.manual_accepts` | Optional. Hand-reviewed rows (`addr decision obj`); they must agree with every method. |
| `inputs.undeterminable` | Optional. Functions every method refused, with `evidence` and `refused_by`. |
| `inputs.manual_names` | Optional. Hand-reviewed names (`addr proposed_name proto_proc evidence …`) for stage M. |
| `inputs.extent_allowlist` | Optional. Addresses where the function boundary is the recompiler's choice (DS2: `config/flaw_sweep_allowlist.tsv`, column 2). They are excluded from the residue stages' known answers and claims. |
| `inputs.source_roots` | Rules TSV (`pattern`, `replacement`) that maps build-machine source paths to tree folders. |
| `pdb.reader_dir` | Directory of the PDB reader (`identity_donor.py` and `identity_image.py`; xb360_toolkit/lanes). |

## Outputs (`output.dir`)
| file | content |
|---|---|
| `allocation.tsv.gz` | One row per registered function: `addr symbol class basis obj lib src_file src_line tree_path name_status pdb_name ident calltype nparams has_this mangled prototype`. Same schema as DS2Recomp `config/allocation_schema.md`. |
| `allocation_owners.tsv.gz` | `addr owner_obj`: the full owner list of every FOLDED function. |
| `names.tsv.gz` | `addr method pdb_name ident proto_va obj_check evidence aliases`: names for file-only functions. Folded procs carry a primary name plus the alias list. |
| `residue.tsv` | `addr kind class reason`: every function without a file (kind `file`) or without a name (kind `name`). The reason is the primary cause: the bracket side that got furthest through the checks. When the two sides differ, every side's reason follows as `(sides: a,b)`. Before 2026-10-07 the reason was the alphabetical minimum. TAIL_OF and allowlisted rows carry those reasons instead. |
| `receipt.json` | Per-stage decided/correct counts, precision, Wilson 95% lower bound, tune and held-out splits, controls, refusal reasons, gate deviations, input and output sha256, and the shakes. It is deterministic: no timestamps. |
| `evidence_map.tsv.gz`, `pdb_<label>/` | Intermediate tables. They are cached; the cache is keyed by the PE and PDB sha256. |

All gzip files use mtime 0. Game-derived output belongs in a private location only. Never commit it to a public repository.

## Config example
```toml
[inputs]
retail_pe = "data/retail.pe"
anchors = "data/anchors.tsv"
source_roots = "config/source_roots.tsv"

[[prototype]]
role = "primary"
label = "final"
pe = "data/proto_final.pe"
pdb = "data/proto_final.pdb"

[[prototype]]
role = "aux"
label = "debug"
pe = "data/proto_debug.pe"
pdb = "data/proto_debug.pdb"

[registered]
adapter = "rexglue"
path = "app/generated"

[pdb]
reader_dir = "C:/path/to/xb360_toolkit/lanes"

[gate]
precision = 0.99          # default bar; every override is logged in receipt.gate_deviations
control = 0.01
salt = "my-title-split-v1"

[gate.overrides.A5_INFERRED]
control = 0.05
reason = "why this stage may run with a looser control"

[allocation]
experimental = []         # e.g. ["FOLDED_TRACED", "AUX_BYTES", "ICF_LEADER"]

[naming]
methods = ["N1", "N2", "N3", "N4", "N5", "N6", "N7", "N9"]
experimental = []         # ["N8"]

[output]
dir = "build/pdbchain"
```
`examples/ds2.toml` is the full DS2 configuration.

## Gate engine (`gates.py`)
Every stage hands the engine its leave-one-out decisions on hidden known answers of the same shape as the population it fills. The decisions come as `(key, correct)` pairs, plus a control that must fail: a shifted target, shuffled labels, another function's evidence, or bytes at +4.

The gate passes only when all of these hold:
- precision ≥ bar on all decisions;
- Wilson 95% lower bound ≥ `gate.wilson` (default 0.99) over all decisions (tune + held when the stage hands its own held-out set);
- precision ≥ bar on the held-out half (a salted sha256 split of the keys);
- control rate < control bar;
- every stage-specific requirement holds.

Stages that tune thresholds (N7, N8) tune on their tune split only and hand the held-out decisions over separately. The Wilson 95% lower bound is always reported.

The engine shakes itself at start-up. A perfect synthetic list must pass. Lists with 2% wrong decisions, with a 2% control, or with an empty held-out half must each fail; otherwise it refuses to run.

Further rules:
- **Unique resolution:** a name, start or obj with zero or several candidates is refused, never picked.
- **Table invariants and shakes:** the allocation check must fail on a copy with one row's obj blanked and on a copy with one row dropped. The names check must fail on a copy with one row moved to the neighbouring proc and on a copy with a duplicated row.

## Stage reference (chain order = strength). DS2 numbers: 47,695 registered functions
| stage | rule | DS2 gate measurement | DS2 admitted |
|---|---|---|---|
| E1 evidence map | Each section contribution's retail offset is voted by the anchors inside it and by `bl` and `lis`+D operand pairs of byte-aligned anchors. bytes EXACT / DELTA / REFUSED. A name needs ≥ 2 independent kinds (pos, call, ref, shape). | Byte LOO 14,271/14,272 (0.99993). Name LOO 14,229/14,229. Shift +4: 4 EXACT vs 38,391. Shuffled pairing: 25 contributions, 20 names. | 199,546 rows |
| keyed feed (input) | Imports by library+ordinal, RTTI through vtable → complete-object locator, `.pdata` begin+size (R1-13b v2) | Imports 278/289; RTTI 1,890/1,902; perturbation controls ≈ 0 | 27,537 feed rows |
| A1 PDB_EXACT / PDB_DELTA | Evidence-map proc row at the start, a keyed row, or the interior of a placed proc | Inherits E1 | 30,867 / 800 |
| A2 LINK_BRACKET | Both bracketing anchors carry one delta d; the code contribution at x−d names the obj | 29,378/29,380; label permutation 418/29,380 (chance floor, control bar 0.05, logged) | 12,972 |
| A3 LINK_RANGE | Inside exactly one contribution whose anchors all imply one retail range | 3,850/3,850 | 339 |
| A4 PDB_BYTES | Strict masked bytes, formed addresses that map through the evidence map, and direct callees in the same obj, against the bracketing objs' procs | Known answers: anchors + exact-start A2 functions (43,229): 2,205/2,211, Wilson 0.99409; control 0/2,702 | 690 |
| A4 PDB_BYTES_GAP / _NOCALL | Same rule, with the contribution gap as candidates / with callee-less bodies allowed | 1,269/1,287; 1,759/1,786 (refused) | 0 |
| A5 INFERRED | Nearest allocated neighbours on both sides agree (fixpoint) | 39,796/39,972; label shuffle 16/549 (control bar 0.05, logged) | 1,494 |
| A6 CLOSEOUT | Masked-byte index over procs and S_THUNK32 with verified refs: LINKER_GEN, IMPORT_THUNK, COMPILER_GEN, PDB_REFS (one owner), FOLDED (owner list is the claim) | All anchors: FOLDED owner-in-list 1,061/1,062 (Wilson 0.99469); IMPORT_THUNK 198/198 (Wilson 0.981, refused, admitted 0 before too); unique LOO refused; shift +4 287/31,814 | 196 FOLDED |
| A7 PDB_DATA_OWNER | Data refs, mapped to PDB data contributions, meet the masked-byte candidates in exactly one obj, with no allocated callee | DEMOTED 2026-10-08: whole population 372/379 = 0.98153, Wilson 0.96237 (was 205/207 on a 4,000 sample) | 0 (56 rows now UNDETERMINABLE, 8 NAME_MODULE) |
| A8 MANUAL_EVIDENCE | Input table, checked against every method | hand review | 4 |
| A9 UNDETERMINABLE | Input list with refusal evidence; no folder | — | 269 |
| N1 offset start | x → proto x−d; a proc starts there in the obj, unclaimed, one name, size and opcode shape ≥ 0.75, no contradicting callee, no passing sibling within ±k (k = 2) | 29,646/29,646; next-proc shift 52/30,806 | 6,707 |
| N2 ordered alignment | Between two named functions of one obj, unnamed functions and unclaimed procs pair 1:1 | 11,164/11,164; shifted window 0/17,692 | 3,376 |
| N3 callee multiset | ≥ 2 named callees; the multiset matches one proc | 804/804; shuffled obj 0/1,500 | 12 |
| N4 refs/strings tie-break | Sibling ambiguity resolved by mapped data addresses, strings and callees (≥ 2 agree, none contradict) | 75/75 + held-out near shape 1,005/1,005: Wilson over all 1,080 decisions ≥ 0.996; control 0/146 | 938 |
| N5 local re-align | Nearest named obj-mates' own deltas | 29,899/29,899; same shape 37/37 | 46 |
| N6 folded primary + aliases | Folded proc start: the alias list is the claim | folded starts 92/92 + the same claim on hidden known answers (true name in the alias list): 30,352/30,375 together, Wilson 0.99886 | 389 |
| N7 inexact opcode similarity | Best token similarity within ±3, margin and ref agreement; thresholds tuned on a crc32 split | held-out 5,907/5,907 (Wilson 0.99935); window moved 31/8,000 | 93 |
| N9 ambiguous-start aliases | Allocation row AMBIGUOUS (evidence-map start carries several folded names): primary + alias list, obj in the owners | LOO true name in alias list 30,212/30,283 (0.99924); next start with a different name set 10/30,283 | 137 |

**Residue stages (`stages/naming_ext.py`, run after N9; `naming.residue_stages`).** These stages use the same engine. Known answers exclude the `extent_allowlist` addresses and the TAIL_OF rows. A stage fills only functions that are still unnamed, and a target proposed by two functions is refused.

| stage | rule | DS2 gate measurement | DS2 admitted |
|---|---|---|---|
| X0 extent normalisation (pre-stage) | Pluggable `naming.extent_normalisers` (default `pdata`, `tail_blr`). `pdata`: a `.pdata` begin takes the raw `.pdata` length. `tail_blr`: a function ending in an unconditional `b`, followed by a registered 4-byte `blr` with no `.pdata` entry, absorbs that `blr`, and the `blr` row becomes `TAIL_OF:<prev>` (never named). The result is a derived extent table; codegen is untouched. | Known answers with exact size: 15,124 → 27,610 of 30,806; with `size_ok`: 30,406 → 30,569 | 284 TAIL_OF rows (118 in the residue) |
| T6 public_code rows | Evidence-map `public_code`/`thunk` row at the retail start, with one proto address | 28,338/28,338; next mapped public 0/28,338 | 503 |
| T2 vtable slots | Evidence-map `vtable` rows. The function-pointer run stops at the first unregistered word. Slot i maps to proto slot i when ≥ 2 other known slots of the table agree and none disagrees (fixpoint, 2 rounds) | 33,286/33,305 (0.99943), held 16,747/16,758; slots shifted by one 0/33,305 | 1,010 |
| T1 thunks by content | Keys: adjustor (`addi r3,r3,-K ; b T`) by K plus T's name; vcall by slot offset; import by the ordinal words. Matched against proto S_THUNK32/S_PUB32 addresses with no proc. Semantic check: the mangled public's `adjustor{K}`/`vcall{off}` must match, and the thunk class (S_THUNK32) must equal T's class. Runs after T2, which supplies its adjustor known answers. | 829/829 (adj 581, imp 198, vcall 50); key perturbed, true thunk recovered 0/834 | 763 |
| T4 trailing-blr extents | The proto proc ends `b ; blr`, the retail words over proto size − 4 are equal under the mask, and the next retail word is the `blr` | 370/370; window shifted 1/370 | 74 |
| T3R single mapped data reference | Retail formed addresses, mapped to proto through the evidence-map data rows (offset kept), are formed by exactly one unclaimed obj proc that passes size/shape/callee | 3,814/3,815; another function's refs 0/4,593 | 473 |
| T3S string text | The retail string texts are a subset of exactly one ±3 window proc's texts | 782/782; another function's strings 0/888 | 23 |
| T5 call-site order | Named callers whose `bl` list has the proto caller's length place x at the proto callee in the same position (fixpoint) | 19,984/19,984; position shifted, true callee 152/30,797. Against existing claims: agrees 21,104, disagrees 1 (an N6 alias) | 57 |
| T7 optimal assignment | Hungarian assignment per gap between named obj-mates: unnamed functions against unclaimed procs. Score = opcode-token similarity under the size/callee checks. A pair is admitted at sim ≥ 0.9 when it beats every alternative by ≥ 0.1. | 7,399/7,399; window moved, wrong pick 18/10,051 | 83 |
| T8 strict-constant tie-break | Sibling ties (>= 2 window procs pass N1's checks): the one candidate that is `strict_equal_words` (struct offsets/constants identical), exact extent, formed data addresses agreeing with the evidence map. LOO hides the whole ±k window's claims so known answers face the same ties | 1,624/1,625 (Wilson 0.99652), held 848/848; window shifted 2/3,062 | 45 |
| T9 RTTI vtable slots | Retail vtables (runs of registered code pointers in data sections with a complete-object-locator before them) keyed by (type-descriptor name, COL offset) = the proto `??_7` public with the same COL key; slot k → proto slot k; one prediction from every vtable holding x | 1,340/1,340 (Wilson 0.99714); slot shifted 0/1,340 | (see receipt) |
| X1 interior pieces | The previous function is placed on proc P with delta d, x−d lies inside P and x's bytes equal P's there: INTERIOR_OF:<prev>, not a function, out of the naming denominator | fires on known separate functions and at bytes+4 must stay < 1% | (see receipt) |
| A8 name → obj | An UNDETERMINABLE row named by an obj-free stage (T1/T2/T5/T6/T9) takes its obj from the name's PDB module and becomes class `NAME_MODULE` | 1,749/1,749 on allocated rows named by those stages; next obj 0/1,657 | 66 |
| M manual evidence | `inputs.manual_names`: hand-reviewed rows, applied only where no gated stage named the function. A row that contradicts a gated stage is refused and listed. | hand review; 2,273 of 2,292 already named identically by the gated stages, 0 contradictions | 19 |

**Opt-in experimental stages.** DS2 refused all of them. Rerun them on a new title, because linker behaviour differs between titles.

| stage | DS2 result |
|---|---|
| `CALLGRAPH_TIEBREAK` (call affinity for two-delta brackets) | 360/389 = 0.925 |
| `FOLDED_TRACED` (caller locality) | 1,627/1,930 = 0.843; shuffled-caller control 15% |
| `LAYOUT_RUN` (surviving fold copy inside its owner's run) | 515/618 = 0.833 |
| `DATA_REFS` (all mapped data refs in one obj) | 887/1,729 = 0.513 |
| `AUX_BYTES` (aux PDBs, bytes only) | q 0.973, d 0.946 |
| `RETAIL_ONLY_FOLDER` (folder from the neighbours' common directory) | ≤ 0.974 at every depth |
| `ICF_LEADER` (earliest link-order copy survives ICF; measurement only) | 0.342 vs random 0.386 |
| `PADDING` (zero words at obj boundaries; measurement only) | not distinguishable (≈ 5% everywhere) |
| `N8` (aux-PDB body similarity as sibling tie-break) | tuning best 148/152, Wilson lower 0.934 |

## Architecture-specific modules
- `arch/ppc.py`: Xenon instruction masks and decoders: relocation masks, masked signature, `code_equal`, strict compare, operand pairs, `bl` targets, formed addresses, opcode tokens. For another ISA, replace this module.
- `readers/pe.py`: PE32 reader (mapped or raw) with Xenon `.pdata`. A failed read is `None`, never zeros.
- `readers/pdb.py`: adapter over the external MSF/DBI/CodeView reader. It yields the procs, code and data contributions, and thunks tables.
- `readers/registered.py`: recompiler adapters (ReXGlue, generic TSV).

## Adding a stage
1. Write the rule as a function of the shared state (`alloc_obj`, anchors, `contrib_at`, readers) in `stages/allocation.py` or `stages/naming.py`. Put it at its strength position in the chain.
2. Measure it first. Hide each known answer of the same shape as your target population, re-derive it, and collect `(key, correct)`. Add a control that must fail.
3. Call `ctx.engine.judge("<ID>", items, control=(population, accepted), require={...}, extra={...})`. If the stage tunes thresholds, tune on a split and pass `heldout_items`.
4. Admit only `if g["gate"]`. Fill only functions that are still unallocated or unnamed, and refuse anything that is not unique.
5. If the stage is unproven, put it in `EXPERIMENTAL` (opt-in). Document its measured numbers in this table.
6. Rerun twice. The second run must print `files changed: 0`.

## Porting to another title
Title-agnostic (keep): the gate engine, every stage rule, the table schemas, the readers' contracts.

Title-specific (redo per title):
- **Config:** every input path, the `code_window`, `[[prototype]]` roles (pick as primary the PDB build closest to retail by linked layout), `source_roots.tsv` (build-machine path prefixes to tree folders).
- **Anchors** (`inputs.anchors`): your own digest/signature matches of retail functions; E1 quality depends on their density.
- **Gate overrides:** DS2's A2/A5 control overrides and E1's stricter bars are DS2 measurements. Delete them, run, and add back only overrides whose reason you measured on your title.
- **Hand inputs:** `manual_accepts` and `undeterminable` come from your own residue review (start with neither; the allocation check then lists every unallocated function).
- **Experimental stages:** rerun every one; linker behaviour (ICF, COMDAT order) differs between titles, so DS2's refusals do not carry over.
- **ISA / recompiler:** replace `arch/ppc.py` for another ISA; add a `readers/registered.py` adapter for another recompiler.

A regression like `regress_ds2.py` for your title (chain output vs your committed tables) is the acceptance test.

## Not ported (inputs instead)
- **XEX → PE:** decryption and decompression are not done here. Give the chain the basefile PE.
- **Keyed methods (R1-13b v2):** imports, RTTI and `.pdata` keyed matching run in `ds2_switch_port/tools/r1_13_pdb_evidence_map_v2.py`. Their output feed is an input here (`inputs.keyed_feed`).
- **Residue audit and hand review:** these produce the `undeterminable` and `manual_accepts` inputs. The tools are `tools/naming/residue_audit.py` and `tools/naming/write_*.py`.
