# Building

## Requirements
- Windows 10/11 x64, Vulkan 1.2+ GPU
- Visual Studio 2022 Build Tools (C++ workload + bundled clang)
- CMake 3.25+, Ninja, git, Python 3.11+ (Pillow for the smoke test)
- Your own Dead Space 2 Disc 1 image or its `default.xex`
  (sha256 `928597ddb80240b8aefeef0a70a9476ee85d6de733035b326dd155c2094a3408`)

## Build
```
python tools\build.py --target tu2 --iso "D:\isos\Dead Space 2 (Disc 1).iso" --xexp "D:\tu2\disc001\default.xexp"
```
1. **SDK:** clones ReXGlue at the pinned commit (`sdk/REXGLUE_BASE.txt`), applies
   `sdk/rexglue_ds2.patch` and installs it to `thirdparty/rexglue-sdk/out/install/ds2-release`.
2. **App:** builds `src_tu2/` into `build/bin/ds2.exe`, `rexruntime.dll` and `rexgpu-xenos.dll`.
3. **XEX:** extracts `default.xex` to `build/bin/game/`, checks the hash, writes `ds2.toml`.

Prints `BUILD_OK <exe>` on success.

| Option | Effect |
|---|---|
| `--xex FILE` | Use an extracted XEX instead of `--iso` |
| `--sdk-prefix DIR` | Reuse an already installed SDK (same pin and patch) |
| `--mods` | Build with the mod API (`DS2_MODS=ON`) |
| `--target tu2`, `--xexp FILE` | TU2 is the only target; the matching title update is required |
| `--build-dir DIR`, `--jobs N` | Output folder, parallel jobs |

### By hand (SDK installed, VS x64 developer prompt)
```
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=clang ^
      -DCMAKE_CXX_COMPILER=clang++ -DCMAKE_CXX_FLAGS=-march=x86-64-v2 ^
      -DCMAKE_PREFIX_PATH=<sdk prefix> -DCMAKE_RUNTIME_OUTPUT_DIRECTORY=%CD%\build\bin
cmake --build build
```
Flags: `-O2`, `-fno-strict-aliasing -ffp-model=strict -fno-char8_t` everywhere; guest code also
gets `-fasync-exceptions -gline-tables-only` and the PCH `src_tu2/ds2_rex_diagnostic_pch.h`.

## Run
`build\bin\ds2.exe` mounts the ISO from `ds2.toml` (or `--ds2_iso=PATH`, or the first `*.iso`
next to the exe) and boots `game/default.xex`.

Defaults the app sets (each can be overridden on the command line):
- `--vulkan_allow_present_mode_immediate=false`: V-Synced presentation. The SDK otherwise picks
  IMMEDIATE, which tears; `--vsync` only paces the game, not the swapchain.
- `--p4_vk_pipeline_cache_path=<cache_root>/vk_pipelines.bin`: compiled pipelines are kept between
  runs (written in the background at most every 10 s), so a shader only stutters the first time.

The game itself runs at 30 FPS, as on the Xbox 360.

Smoke test (use absolute paths):
```
python tools\smoke_test.py C:\path\to\build\bin\ds2.exe C:\path\to\build\smoke
```
Prints `SMOKE_RENDERED` if the frame at 70 s is non-blank and the game exits cleanly.

Crash reports: configure with `-DDS2_CRASHDUMP=ON` (off by default). On an unhandled access
violation the game writes `ds2_crash_N.txt` (registers and host stack as module offsets) and
`ds2_crash_N.dmp` to its working directory, and the linker writes `bin\ds2.map`. Resolve the
stack to recompiled function names with
`python tools\symbolize_crash.py build\bin\ds2.map ds2_crash_0.txt`.

## DLC (Severed)
```
python tools\extract_dlc.py "<package file>" --user-root C:\path\to\user
build\bin\ds2.exe --user_data_root=C:\path\to\user
```
`extract_dlc.py` reads the package's own file table. It writes to
`<user root>\0000000000000000\<title id>\<content type>\<package file name>\`, the layout the runtime mounts, and checks
every file's size. `--list` lists the contents without writing anything. The DLC also needs the fix in
`sdk/rexglue_ds2.patch`: without it, ReXGlue read a missing content header as success and the game showed a disc read
error. Status: working, but not fully playtested.

## Title Update #2 (`--target tu2`)
```
python tools\build.py --target tu2 --iso "D:\isos\Dead Space 2 (Disc 1).iso" --xexp "D:\tu2\disc001\default.xexp"
```
- A title update changes the code, so it has its own recompiled tree, `src_tu2/`.
- `--xexp` is pinned (sha256 `8d386d37…01ec2`). It's staged as `build\bin\game\default.xexp`, and the loader applies it at runtime.
- The ISO stays mounted as `d:` for all game data, and `game:` is the `game\` folder (XEX plus patch).
- The TU2 build requires its matching `.xexp`. Retail-only builds are no longer offered.
- Disc 2 with its own `disc002\default.xexp` patches to the same image, so the TU2 build runs it too.

## Verify against the pipeline build
`tools/diagnostics/compare_objs_exact.py` compares guest objects byte for byte (ignoring debug
sections) against a DS2Recomp build; expected result `EXACT PASS` 159/159. Each object embeds
the PCH path, so add `-fno-file-reproducible -fmacro-prefix-map=<this>/src=<DS2Recomp>/app/generated`
to `CMAKE_CXX_FLAGS` when the checkouts live at different paths.

## Launcher
Install PyInstaller in your Python build environment, then run `python tools/build_launcher.py`. The frozen launcher needs the TU2 runtime and runtime-manifest.json beside it. Download the playtest package for the existing runtime.
