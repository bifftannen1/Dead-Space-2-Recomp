# Modding

C++ mods can hook game functions by their PDB name, overlay loose files on the disc, and read
game structures through headers generated from the PDB. The mod API only exists in a mods
build; the normal build contains none of it.

| Piece | Where |
|---|---|
| Hook API | `src/modapi/ds2_modapi.h` |
| Big-endian guest types | `src/modapi/ds2_be.h` (`ds2::be<T>`, `ds2::gptr<T>`, `ds2::guest<T>()`) |
| Loader + file overlay | `src/modapi/ds2_mod_loader.h`, `src/app/main_mods.cpp` |
| Build glue | `src/modapi/modapi.cmake` |
| Generators | `tools/modapi/gen_hooks.py`, `tools/modapi/gen_types.py` |
| Name table | `tools/modapi/symbols_lane5_admitted.tsv` (va, size, name, mangled) |
| Disc helpers | `tools/modapi/disc_files.py`, `tools/modapi/make_loose_demo.py` |

Generated files (hook tables, PDB headers, loose-file samples) are written at build time and
gitignored.

## Build and run
```
python tools\build.py --iso <disc 1 iso> --mods
```
Optional PDB struct headers: add `-DDS2_TYPES_PDB=<path>\deadspace_f.pdb` when configuring.
`mods/` is copied next to `ds2.exe`. The log (`bin\logs\ds2_NNN.log`) lists manifests, active
hooks and conflicts under `ds2-mods:`.

| Cvar (command line, `ds2.toml` or `REX_<NAME>`) | Default | Meaning |
|---|---|---|
| `ds2_mods` | `true` | `false` disables every mod |
| `ds2_mods_dir` | `<exe>\mods` | Manifest folder |
| `ds2_mods_trace_opens` | `false` | Log every disc file the game opens |

## Manifest: `mods/<name>.toml`
```toml
[mod]
name = "fov"
version = "1.0.0"
enabled = true            # false: compiled in but inactive
load_order = 10           # ascending, ties by name
sources = ["fov/fov.cpp"]
hooks = ["RenderContext::RC_RebuildAndApplyProjectionMatrix"]
calls = []                # guest functions called with DS2_CALL
files = "fov/files"       # optional loose-file root

[settings]                # read with DS2_SETTING_NUMBER/BOOL/STRING
fov_scale = 1.25
```
The manifest must list every hook and call the sources use; the build fails otherwise.

## Hooks
```cpp
#include "ds2_modapi.h"

static void After(ds2::mod::Call& c) {
  c.ctx.f1.f64 *= DS2_SETTING_NUMBER("scale", 1.0);
}
DS2_HOOK_AFTER("EARS::Framework::CameraManager::GetActiveCamFov", After);
```
| Macro | Runs |
|---|---|
| `DS2_HOOK_BEFORE(name, fn)` | Before the original; may change args in `c.ctx` (r3–r10, f1–f13) |
| `DS2_HOOK_AFTER(name, fn)` | After; result in `c.ctx.r3`/`f1`, entry args in `c.entry_r`/`c.entry_f` |
| `DS2_HOOK_REPLACE(name, fn)` | Instead; `c.call_original()` runs the original |
| `DS2_CALL(c, name, args...)` | Calls a guest function, returns r3 |

`c.load<T>(ea)`, `c.load_f32(ea)` and `c.c_str(ea)` read guest memory with the byte swap.

- Names must resolve to exactly one function. For shared names, write `Name@0xVA` or the
  mangled name; the build lists the candidates.
- Hooks override the game's weak function symbols at link time. The recompiled game objects
  stay byte-identical with or without mods.
- Two mods replacing the same function, or shipping the same file, is logged; the later mod wins.

## Loose files
Put files under the mod's `files` root using game paths
(`mods/loose_demo/files/disc1_info.txt` → `game:\disc1_info.txt`). The disc holds only
`default.xex`, `disc1_info.txt`, `bigfile0.viv` and `bigfile1.viv`; overriding files *inside* a
VIV isn't supported yet, but a whole VIV can be replaced.

## PDB structs
`gen_types.py --pdb deadspace_f.pdb --out <dir> --compile-test` writes `ds2_types.h`: every
PDB class, struct, union and enum in `ds2::pdb` at the PDB's offsets (checked with
`static_assert`s), plus vtable structs.
```cpp
auto* rc = ds2::guest<ds2::pdb::RenderContext>(c.base, c.ctx.r3.u32);
rc->perspective.fovRadians *= 1.25f;   // big-endian load/store
```

## Sample mods
| Mod | Effect |
|---|---|
| `fov` | Scales the field of view (`fov_scale`) |
| `loose_demo` | Replaces `disc1_info.txt`; run `make_loose_demo.py <iso>`, then enable |
| `intro_skip` | Ends the boot movie early. **Disabled:** skipping before ~6 s breaks the title → menu handoff |

## New mod
1. Find the function in `tools/modapi/symbols_lane5_admitted.tsv`; read its body under `src/`
   to see which registers carry the arguments.
2. Add `mods/<name>/<name>.cpp` and `mods/<name>.toml`.
3. Build with `--mods` and check the `ds2-mods:` log lines.
