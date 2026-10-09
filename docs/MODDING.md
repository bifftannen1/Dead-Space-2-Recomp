# Modding Dead Space 2 (PC recomp)

The mod API lets C++ mods hook guest (game) functions by their PDB name, lay loose files over
the disc, and read game structures with headers generated from the PDB. It is compiled only
in a mods build (`DS2_MODS=ON`); the plain build does not contain any of it.

| Piece | Where |
|---|---|
| Hook API (header-only) | `app/modapi/ds2_modapi.h` |
| Big-endian guest memory types | `app/modapi/ds2_be.h` (`ds2::be<T>`, `ds2::gptr<T>`, `ds2::guest<T>()`) |
| Mod loader, file overlay | `app/modapi/ds2_mod_loader.h`, `app/src/main_mods.cpp` |
| Build glue | `app/modapi/modapi.cmake` (included by `app/CMakeLists.txt` when `DS2_MODS=ON`) |
| Generators | `tools/modapi/gen_hooks.py` (hook names), `tools/modapi/gen_types.py` (PDB types) |
| Build driver | `tools/modapi/build_mods.py` |
| Disc helpers | `tools/modapi/disc_files.py`, `tools/modapi/make_loose_demo.py` |
| Sample mods | `mods/fov.toml` (enabled), `mods/loose_demo.toml` (needs your disc), `mods/intro_skip.toml` (disabled, known issue: section 6) |

Nothing game-derived is committed. The hook tables, the PDB headers and the loose-file sample
are written at build time into the build tree or into `mods/*/files/`, and both are gitignored.

## 1. Build with mods

1. Build the game once as the README says: `build.bat --iso <your disc 1 iso>`. That writes
   `app/generated/`, stages the XEX and installs the SDK.
2. Build the mods build:
   ```bat
   python tools\modapi\build_mods.py --mods on --stage-from build\bin ^
       --types-pdb "<your>\deadspace_f.pdb"
   ```
   `--types-pdb` is optional. It generates the PDB struct headers (section 5). The build goes
   to `build\app_mods\bin\ds2.exe`, with `mods\` copied next to it, plus `game\` and `ds2.toml`
   from `build\bin`. It ends with `BUILD_MODS_OK <exe>`.
3. Run `build\app_mods\bin\ds2.exe`. The log (`bin\logs\ds2_NNN.log`) lists every manifest,
   every active hook and every conflict, all under `ds2-mods:`.

Runtime switches (cvars, given on the command line, in `ds2.toml` or as `REX_<NAME>`):

| Cvar | Default | Meaning |
|---|---|---|
| `ds2_mods` | `true` | `false`: load no manifest. Every hook passes straight to the game's code. |
| `ds2_mods_dir` | `<exe>\mods` | Folder that holds the manifests |
| `ds2_mods_trace_opens` | `false` | Log every disc file the game opens (mounts the overlay even with no files) |

## 2. A manifest: `mods/<name>.toml`

```toml
[mod]
name = "fov"              # unique; [A-Za-z0-9_.-]
version = "1.0.0"
description = "..."
enabled = true            # runtime: false keeps the compiled hooks inactive
load_order = 10           # ascending; ties broken by name
sources = ["fov/fov.cpp"] # build time: compiled into ds2.exe with -DDS2_MOD_ID="fov"
hooks = ["RenderContext::RC_RebuildAndApplyProjectionMatrix"]  # every function the sources hook
calls = []                # every guest function the sources call with DS2_CALL
files = "fov/files"       # optional: loose-file root, relative to the mods folder

[settings]                # free-form; read with DS2_SETTING_NUMBER/BOOL/STRING
fov_scale = 1.25
```

The manifest has to be complete. `gen_hooks.py` refuses a source that hooks or calls a name
the manifest does not list, and a listed hook that no source uses.

## 3. Hooks

```cpp
#include "ds2_modapi.h"

static void After(ds2::mod::Call& c) {
  c.ctx.f1.f64 *= DS2_SETTING_NUMBER("scale", 1.0);   // float return value
}
DS2_HOOK_AFTER("EARS::Framework::CameraManager::GetActiveCamFov", After);
```

| Macro | Runs |
|---|---|
| `DS2_HOOK_BEFORE(name, fn)` | before the original; it may change the arguments in `c.ctx` (r3..r10, f1..f13) |
| `DS2_HOOK_AFTER(name, fn)` | after the original; the result is in `c.ctx.r3` / `c.ctx.f1`, and the entry arguments in `c.entry_r[3..10]` / `c.entry_f[1..4]` |
| `DS2_HOOK_REPLACE(name, fn)` | instead of the original; `c.call_original()` runs it |
| `DS2_CALL(c, name, args...)` | calls a guest function (r3.. = args) on a copy of the registers and returns r3 |

`fn` is `void fn(ds2::mod::Call&)`. `c.base` is guest memory. `c.load<T>(ea)`, `c.load_f32(ea)`
and `c.c_str(ea)` read it with the byte swap.

How a name becomes an address:
- At configure time, `gen_hooks.py` resolves every name in the manifests against
  `config/symbols_lane5_admitted.tsv` (the retail PDB name table).
- A name must resolve to exactly one address. When several functions share a name, the tool
  refuses the name and lists the candidates. Spell one of them as `Name@0xVA` or by its
  mangled name.
- The address must also be a function in the codegen table
  (`app/generated/*_register.cpp`). Configure fails otherwise.
- `ds2::mod::hook_addr()` is `consteval`, so a name that is not in the generated table is a
  compile error.

How a hook is installed:
- The generated code defines every guest function `sub_<VA>` as a weak alias of
  `__imp__sub_<VA>` (`DEFINE_REX_FUNC`). The generated `ds2_hook_dispatch.cpp` defines a
  strong `sub_<VA>` for each hooked address. That strong definition wins at link time, which
  is the SDK's own override mechanism (`rex/hook.h`, `REX_HOOK`). Direct calls and the
  function table both reach it.
- With the named tree (`DS2_NAMED_TREE=ON`; `tools/modapi/build_mods.py --named`), the symbol is
  the PDB-derived identifier (e.g. `RenderContext__RC_RebuildAndApplyProjectionMatrix`, not
  `sub_8250D2A8`; docs/NAMED_TREE.md). `gen_hooks.py` takes each hooked address's symbol from the
  compiled registration table (`app/generated_named/ds2_rex_diagnostic_register.cpp`), so the
  dispatcher overrides the right weak symbol. Mods still name hooks by PDB name; nothing in a
  mod changes.
- No generated file is edited: the 157 recomp objects are byte-identical with and without
  mods.
- An address without enabled handlers costs one atomic load and a call.

Load order and conflicts:
- BEFORE and AFTER handlers run in load order.
- When two enabled mods REPLACE the same function, that is a conflict. It is logged and the
  later mod wins.
- A handler whose mod has no manifest, or has `enabled = false`, never runs.

## 4. Loose files

- Put files under the mod's `files` root, using the game's paths. For example,
  `mods/loose_demo/files/disc1_info.txt` answers `game:\disc1_info.txt`.
- At startup the loader builds a merged entry tree over the device `game:` points at (the
  disc image or an extracted folder). It registers that tree as `\Device\Ds2Overlay0` and
  re-links `game:` and `d:` to it.
- The disc device is not touched. With no enabled file layers nothing is mounted.
- When two mods provide the same path, that is a conflict. It is logged and the later mod
  wins.
- Each replaced file is logged the first time the game opens it, with its host path, size and
  fnv1a64.
- Limits:
  - The disc holds `default.xex`, `disc1_info.txt` and two archives (`bigfile0.viv`,
    `bigfile1.viv`), as `tools/modapi/disc_files.py <iso> list` shows. Everything else, movies
    included, lives inside the VIVs and is read by the game's own archive code.
  - Overriding files inside a VIV is not implemented. It would need hooks on
    `EARS::Framework::RealFileSystem`. A whole VIV can be replaced as a loose file.

Sample: `python tools\modapi\make_loose_demo.py <iso>` copies `disc1_info.txt` out of your
disc with one CRLF appended. Then set `enabled = true` in `loose_demo.toml`.

## 5. Game structures from the PDB

`gen_types.py --pdb deadspace_f.pdb --out <dir> --compile-test` (or `build_mods.py --types-pdb`)
writes `ds2_types.h`. It contains:
- every class, struct and union with a full definition, in `ds2::pdb`, with fields at the
  PDB's offsets (`ds2::be<T>` scalars, `ds2::gptr<T>` pointers);
- the enums, as `enum class` with their underlying type;
- a `<Type>_vtbl` struct for each class that introduces virtual functions.

For example:

```cpp
#include "ds2_types.h"
auto* rc = ds2::guest<ds2::pdb::RenderContext>(c.base, c.ctx.r3.u32);
float fov = rc->perspective.fovRadians;      // big-endian load
rc->perspective.fovRadians = fov * 1.25f;    // big-endian store
```

Names are flattened: `A::B<C>` becomes `A__B_C_`, with the PDB name in a comment. When two
different definitions share a name, both are emitted with a `__ti<index>` suffix. Each run also
checks its own output:
- `ds2_types_test.cpp` holds a `static_assert` for every `sizeof` and every field `offsetof`,
  checked against the PDB;
- a sample of types is cross-checked against a second reader (`llvm-pdbutil pretty`, DIA).

## 6. The sample mods

| Mod | Hooks | Effect |
|---|---|---|
| `fov` | BEFORE+AFTER `RenderContext::RC_RebuildAndApplyProjectionMatrix` | scales `perspective.fovRadians` for the projection build, then restores it (`settings.fov_scale`) |
| `intro_skip` (disabled, known issue) | AFTER `EARS::RtMovie::RtMoviePlayer::Play`, AFTER `rw::movie::MoviePlayer2::IsFinished`, BEFORE `RtMoviePlayer::Stop` | ends the boot movie `trio_frontend.vp6` early, on its own player only. **Known issue: breaks the title -> menu logo handoff.** The game still boots while the logos play; ending the movie before about 6 s of it has played leaves the title screen stuck (prompt gone, Start ignored) or the title logo over the main menu. Kept as hook-API source only; `enabled = false`. |
| `loose_demo` | none | `disc1_info.txt` from `mods/loose_demo/files` |

## 7. Writing a new mod, step by step

1. Find the function: grep `config/symbols_lane5_admitted.tsv` (columns va, size, name,
   mangled) for the name. A name shared by several functions must be written `name@0xVA`. Read its body in `app/generated/*.cpp`
   (`DEFINE_REX_FUNC(sub_<VA>)`) to see which registers carry the arguments.
2. Create `mods/<name>/<name>.cpp` with `DS2_HOOK_*` and `mods/<name>.toml` listing `sources`,
   `hooks` and `calls`.
3. Run `python tools\modapi\build_mods.py --mods on --stage-from build\bin`. A wrong or
   ambiguous name fails configure with `GEN_HOOKS_FAIL` and the candidates.
4. Run the game and check the `ds2-mods:` lines in the log.
5. To turn a mod off without rebuilding, set `enabled = false`. To turn all mods off, pass
   `--ds2_mods=false`.

6. Check that a mod did not change anything else: build with mods off and on and run
   `python tools/diagnostics/compare_objs_exact.py --a build/app_nomods/CMakeFiles/ds2_rex_diagnostic_recomp.dir/generated --b build/app_mods/CMakeFiles/ds2_rex_diagnostic_recomp.dir/generated --b-glob "*_recomp.*.cpp.obj"`
   (must print `EXACT PASS`, 157/157: mods never change the recompiled game code).
7. Screenshots without stealing input: `docs/evidence/modapi-2026-10-07/run_ab.py` runs a copy
   with every pad key bound to F13-F24 and posts only those keys.

Load order and conflicts: manifests load by ascending `load_order`, then name. A broken
manifest, a duplicate mod name, two REPLACE hooks on one function and two mods shipping the same
file are each reported in the log (`ds2-mods: N conflicts/errors` and one line each); for files
the later mod in load order wins.

DLL plugins are not implemented. Mods are static plugins, compiled into `ds2.exe`.
