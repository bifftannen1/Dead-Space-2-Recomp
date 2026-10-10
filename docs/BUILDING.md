# Building

## Requirements
- Windows 10/11 x64, Vulkan 1.2+ GPU
- Visual Studio 2022 Build Tools (C++ workload + bundled clang)
- CMake 3.25+, Ninja, git, Python 3.11+ (Pillow for the smoke test)
- Your own Dead Space 2 Disc 1 image or its `default.xex`
  (sha256 `928597ddb80240b8aefeef0a70a9476ee85d6de733035b326dd155c2094a3408`)

## Build
```
python tools\build.py --iso "D:\isos\Dead Space 2 (Disc 1).iso"
```
1. **SDK:** clones ReXGlue at the pinned commit (`sdk/REXGLUE_BASE.txt`), applies
   `sdk/rexglue_ds2.patch` and installs it to `thirdparty/rexglue-sdk/out/install/ds2-release`.
2. **App:** builds `src/` into `build/bin/ds2.exe`, `rexruntime.dll` and `rexgpu-xenos.dll`.
3. **XEX:** extracts `default.xex` to `build/bin/game/`, checks the hash, writes `ds2.toml`.

Prints `BUILD_OK <exe>` on success.

| Option | Effect |
|---|---|
| `--xex FILE` | Use an extracted XEX instead of `--iso` |
| `--sdk-prefix DIR` | Reuse an already installed SDK (same pin and patch) |
| `--mods` | Build with the mod API (`DS2_MODS=ON`) |
| `--build-dir DIR`, `--jobs N` | Output folder, parallel jobs |

### By hand (SDK installed, VS x64 developer prompt)
```
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=clang ^
      -DCMAKE_CXX_COMPILER=clang++ -DCMAKE_CXX_FLAGS=-march=x86-64-v2 ^
      -DCMAKE_PREFIX_PATH=<sdk prefix> -DCMAKE_RUNTIME_OUTPUT_DIRECTORY=%CD%\build\bin
cmake --build build
```
Flags: `-O2`, `-fno-strict-aliasing -ffp-model=strict -fno-char8_t` everywhere; guest code also
gets `-fasync-exceptions -gline-tables-only` and the PCH `src/ds2_rex_diagnostic_pch.h`.

## Run
`build\bin\ds2.exe` mounts the ISO from `ds2.toml` (or `--ds2_iso=PATH`, or the first `*.iso`
next to the exe) and boots `game/default.xex`.

Smoke test (use absolute paths):
```
python tools\smoke_test.py C:\path\to\build\bin\ds2.exe C:\path\to\build\smoke
```
Prints `SMOKE_RENDERED` if the frame at 70 s is non-blank and the game exits cleanly.

## Verify against the pipeline build
`tools/diagnostics/compare_objs_exact.py` compares guest objects byte for byte (ignoring debug
sections) against a DS2Recomp build; expected result `EXACT PASS` 159/159. Each object embeds
the PCH path, so add `-fno-file-reproducible -fmacro-prefix-map=<this>/src=<DS2Recomp>/app/generated`
to `CMAKE_CXX_FLAGS` when the checkouts live at different paths.
