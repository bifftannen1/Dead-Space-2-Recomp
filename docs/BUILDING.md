# Building

## Requirements
- Windows 10/11 x64, Vulkan 1.2+ GPU driver
- Visual Studio 2022 Build Tools with the C++ workload and the bundled clang (`VC\Tools\Llvm`)
- CMake 3.25+, Ninja, git, Python 3.11+ (Pillow only for `tools/smoke_test.py`)
- Your own Dead Space 2 disc 1 image, or its `default.xex` (sha256
  `928597ddb80240b8aefeef0a70a9476ee85d6de733035b326dd155c2094a3408`)

## One command
```
python tools\build.py --iso "D:\isos\Dead Space 2 (Disc 1).iso"
```
1. **SDK**: clones `rexglue-sdk` at the commit pinned in `sdk/REXGLUE_BASE.txt` (v0.10.0,
   `c94f5ebd`) with submodules into `thirdparty/rexglue-sdk`. It checks the sha256 of
   `sdk/rexglue_ds2.patch` and applies it (a rerun detects it as already applied). Then it
   builds and installs the SDK (Ninja, Release, clang, Vulkan on, D3D12 and Tracy off,
   `-march=x86-64-v2`) into `thirdparty/rexglue-sdk/out/install/ds2-release`.
2. **App**: `cmake -S . -B build` + `cmake --build build`. It compiles `src/` as checked in
   (159 guest TUs + the host app) and writes `build/bin/ds2.exe`, `rexruntime.dll` and
   `rexgpu-xenos.dll`.
3. **XEX**: extracts or copies your `default.xex` to `build/bin/game/`, checks it against the
   pinned hash, and writes `build/bin/ds2.toml` with `ds2_iso`.

The script prints `BUILD_OK <exe>` only when all three binaries exist.

Options:
- `--sdk-prefix DIR` reuses an SDK you already built and installed (from the same pin and patch).
- `--xex FILE` uses an XEX instead of `--iso`.
- `--mods` builds with the mod API (`DS2_MODS=ON`).
- `--build-dir DIR`, `--jobs N`.

## By hand (SDK already installed)
From a VS 2022 x64 developer prompt:
```
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=clang ^
      -DCMAKE_CXX_COMPILER=clang++ -DCMAKE_CXX_FLAGS=-march=x86-64-v2 ^
      -DCMAKE_PREFIX_PATH=<sdk install prefix> -DCMAKE_RUNTIME_OUTPUT_DIRECTORY=%CD%\build\bin
cmake --build build
```

## Compile flags
These are the flags of the working PC build, and they match DS2Recomp's named build:
- `-O2 -DNDEBUG` (Release; not the SDK's `-O3`)
- `-fno-strict-aliasing -ffp-model=strict -fno-char8_t` on every TU
- guest TUs also get `-fasync-exceptions -gline-tables-only` (guest SEH) and the PCH
  `src/ds2_rex_diagnostic_pch.h`

## Running
Run `build\bin\ds2.exe`. It mounts the ISO named in `ds2.toml` (or `--ds2_iso=PATH`, or the
first `*.iso` next to the exe) and boots `game/default.xex`. Smoke test:
```
python tools\smoke_test.py build\bin\ds2.exe build\smoke
```
It prints `SMOKE_RENDERED` when the last frame (70 s) is non-blank and the process exits
cleanly on WM_CLOSE.

## Verifying against DS2Recomp
To check that the guest objects match DS2Recomp's named build, ignoring `.debug$*`:
```
python tools\diagnostics\compare_objs_exact.py ^
  --a <DS2Recomp build>\CMakeFiles\ds2_rex_diagnostic_recomp.dir\generated_named\_units --a-glob "*_unit.*.cpp.obj" ^
  --b build\CMakeFiles\ds2_rex_diagnostic_recomp.dir\src\_units ^
  --support-a <DS2Recomp build>\CMakeFiles\ds2_rex_diagnostic_recomp.dir\generated_named ^
  --support-b build\CMakeFiles\ds2_rex_diagnostic_recomp.dir\src
```
Each object holds one string with the absolute path of `ds2_rex_diagnostic_pch.h` (a
`__FILE__` in the PCH). So two checkouts at different paths differ only in that `.rdata`
string. Add `-fno-file-reproducible -fmacro-prefix-map=<this>/src=<DS2Recomp>/app/generated`
to `CMAKE_CXX_FLAGS` to equalise it for the comparison. Use forward slashes; without
`-fno-file-reproducible`, clang writes the remapped path with backslashes. With these flags
the comparison gives `EXACT PASS` 159/159 against DS2Recomp `fac9d8c`'s named build.
