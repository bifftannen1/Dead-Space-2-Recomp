# ReXGlue codegen: packing builders write vD element by element, which breaks when vD aliases a source

**File:** `src/codegen/builders/vector.cpp` (pinned SDK; file sha256 `b3528ed4a7e0642e2f390cf3d745e18d6e496f14d1707afef6f7049bb84fb39d`)

## Defect
Several builders write the destination one element at a time and keep reading the source
registers after they start writing. When vD is the same register as vA or vB, the result is
wrong. Real code does this, for example `vpkuwus128 v63,v61,v63` and `vpkuwus128 v62,v62,v61`
in Dead Space 2's EA VP6 decoder. Halfwords 4, 6 and 7 come out as FFFF, so whole columns of
every 8-pixel block collapse and the movies render with green blocks.

Affected builders (pre-change line numbers):
- `build_vpkuwus` (1124): vD == vA or vD == vB.
- `build_vpkuhus` (1096): vD == vA or vD == vB.
- `build_vpkd3d128`: pack type 3 FLOAT16_2 (1284), type 4 NORMSHORT4 (1312) and type 5 FLOAT16_4 (1333), when vD == vA.

Checked and not affected: element loops whose reads use the same index as the write
(vlogefp, vsubsws, vsrb, vsrab, vrlw, vsraw, vsrw), vpkd3d128 types 0, 1, 2 and 6 (they
build in vTemp/temp and write vD last), and all vupkd3d128 cases.

## Minimal test
`tools/r1_18_alias_class_test.cpp` (it supersedes `tools/r1_17_vpkuwus_alias_test.cpp`).
Each builder runs a no-alias control plus vD==vA and vD==vB cases. The results are compared
against a reference computed from the VMX/VMX128 semantics on unmodified copies of the sources.
- The old emitted form matches the reference on every control and mismatches on every alias case (5 builders/types, 7 alias cases).
- The patched form matches on all 12 cases. The test prints `R1_18_ALIAS_CLASS_PASS` at both -O0 and -O2 with clang 19.

## Patch
`vector_cpp_alias_fix.patch` (5 hunks, +34/-20):
- vpkuwus and vpkuhus build the result in `vTemp`, then assign `vD = vTemp;`. This is the same idiom vupkd3d128 case 0 already uses.
- vpkd3d128 types 3, 4 and 5 copy the source into a scoped local first: `{ PPCVRegister vPkSrc = vA; ... }`. vD is a partial read-modify-write target, so it cannot be built in a temporary.

## Effect on DS2 output
- Only the 9 instruction sites of these builders changed: 3 vpkuwus128 and 6 vpkd3d128 (types 3/5).
- Two functions gained a `PPCVRegister vTemp{};` declaration, because the codegen declares vTemp only where it is used.
- The function-to-file partition moved because body byte sizes changed. A re-run of the unpatched codegen reproduced v8 byte for byte, so the old output is deterministic and the move comes from the patch.
- The VP6 movie output went from green-block corruption to clean frames.
