# Named tree

`src/` holds the ReXGlue output reorganised with the game's PDB: each function sits in a file
under its original source path and is called by its real name instead of `sub_<ADDR>`.
Registration by address, the machine code and the PPC ABI are unchanged.

## Identifiers
The PDB qualified name with `::` → `__` and other non-identifier characters → `_`
(`hkpConstraintData::getConstraintInfoUtil` → `hkpConstraintData__getConstraintInfoUtil`).
The `_<ADDR>` suffix is kept for:
1. free functions, so they can't clash with host symbols (`memcpy`, `XamUserGetName`, …);
2. names shared by several functions (overloads, folded copies);
3. names equal to another codegen symbol.

Unnamed functions stay `sub_<ADDR>`. Folded functions use their first sorted name, with the
others in a `folded aliases` comment. `src/symbols.tsv` (`addr, codegen_symbol, symbol`) is the
full map.
```cpp
// codegen symbol: sub_822E8CD8 (registered by address 0x822E8CD8)
DEFINE_REX_FUNC(hkpConstraintData__addInstance) {
	...
	hkString__memSet(ctx, base);
```

## Layout
| Dir | Origin | Functions | Files |
|---|---|---|---|
| `DeadSpace/` | game code | 15,869 | 1,230 |
| `EARS/` | EA engine packages | 12,396 | 844 |
| `EA/` | other EA packages (eathread, eastl, Blaze, rwaudiocore, …) | 7,716 | 743 |
| `Havok/` | Havok 6.5 | 5,681 | 729 |
| `XDK/` | XDK libraries, CRT, import thunks | 3,824 | 596 |
| `BFX/` | BFX 3.0 | 1,664 | 78 |
| `_units/` | 157 compile units; 203 unplaceable functions kept inline | | 157 |

Files keep their original path with `.cpp` appended when needed (`ldo.c` → `ldo.c.cpp`).

## Why compile units
Splitting functions across different translation units changed clang's output for 22
functions (the vectoriser depends on TU neighbours). So each `_units/…unit.N.cpp` includes the
named files of codegen TU N in the original order, and clang sees identical TUs.

Checks: every function appears exactly once with a byte-identical body; all 157 objects match
the flat codegen build byte for byte (`compare_objs_exact.py`); the smoke test renders.

## Coverage
Of 47,695 functions, 46,578 carry a PDB name. Each name and file placement comes from a
measured method (exact PDB match, link-order bracketing, prototype-PDB alignment, callee and
data-reference matching), admitted only at ≥ 0.99 measured precision. Functions no method
could place reliably stay unnamed or in their unit file. Per-method numbers, controls and
the full residue audit live in the DS2Recomp pipeline repo.
