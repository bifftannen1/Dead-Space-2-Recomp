# Building Dead Space 2 for PS5

Built on Linux (Ubuntu 24.04). The PS5 build follows the
[GTA IV PS5 port](https://github.com/elripalda/gta4-ps5)'s: the same toolchain, the same
kind of static program, the same link and packaging steps.

> **Status: not run on a console yet.** Every source file of the SDK and the app passes a
> syntax-only compile for the PS5 target (see [Checks](#checks)), but the full build, the
> link and the game itself on a PS5 are untested. Expect the first runs to need fixes; the
> app writes a log made for that ([On the PS5](#on-the-ps5)).

## What you need

- clang 18 or 19, lld, CMake 3.25+, Ninja, Python 3, git
- Nazky's [ps5-plume-triangle](https://github.com/Nazky/ps5-plume-triangle), built once with
  `tools/build.sh` (it fetches and builds the PS5 payload SDK, the PS5 Vulkan driver
  (mihawk-99's PS5_Mesa RADV), the app start-up code and the signer; its README lists what
  that needs: meson, glslang, bison, flex, python3-mako/ply/yaml, rsync)

No game files are needed to build: the recompiled code is already in the repo (`src_tu2/`).

## 1. Source

```
git clone https://github.com/chonzytron/Dead-Space-2-PS5
cd Dead-Space-2-PS5
ps5/tools/fetch_sdk.sh
```

`fetch_sdk.sh` clones ReXGlue at the commit the PC build pins (`sdk/REXGLUE_BASE.txt`) into
`thirdparty/rexglue-sdk-ps5` and applies `sdk/rexglue_ds2.patch` (the Dead Space 2 fixes, as
on the PC), `sdk/rexglue_ps5.patch` (the PS5 platform layer) and `sdk/ffmpeg_ps5.patch`.

## 2. Build

```
TRI=~/ps5-plume-triangle
cmake -S ps5 -B out/ps5 -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_TOOLCHAIN_FILE=$PWD/ps5/cmake/ps5-toolchain.cmake \
  -DPS5_PAYLOAD_SDK=$TRI/.deps/native/ps5-payload-sdk \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5
ninja -C out/ps5 ds2

ps5/tools/ps5_package.sh out/ps5 $TRI
```

The app ends up in `out/ps5/ps5-app/PPSA94108`. Copy that folder to `/data/homebrew/` on the
PS5.

The recompiled game is 157 large files; the first build takes a long time and several GB of
memory per compiler job (`ninja -j4` if the machine runs out).

| Option | Effect |
|---|---|
| `-DDS2_PS5_THINLTO=ON` | ThinLTO across the whole program (slower link, faster game code). Off by default. |
| `-DDS2_OPT=-O3` | Optimisation level of the recompiled code (`-O2` is the PC build's tested default) |
| `PS5_WHOLE_ARCHIVE=0 ps5/tools/ps5_package.sh ...` | Link the runtime and GPU backend as plain archives instead of whole |
| third argument of `ps5_package.sh` | Another title id |

## On the PS5

Everything is in the app's folder, `/data/homebrew/PPSA94108/`:

- `install/`: your Disc 1 image (and Disc 2), Title Update #2, optionally Severed
  (`install/README.txt` says exactly what). The first start prepares the game from them.
- `game/`: `default.xex` and the Title Update #2 patch per disc, taken from your files.
- `UserData/`: saves, installed DLC, the shader and pipeline caches.
- `ds2.toml`: settings (copy `ds2.example.toml`).
- `logs/`: one log per start.

When something goes wrong the log is what to look at:

- `ps5 diag ...` every 10 s: frames presented, memory faults handled, vblanks, GPU command
  batches. A black screen's log shows which of them stopped moving.
- `ps5 stack: ...` when no frame was presented for 30 s: where every thread is.
- `[ps5 crash] ...` as the last line: a thread crashed, with the code addresses on its stack.

The addresses are relative to the program. `ps5_package.sh` keeps the program with its
symbols; this prints the same lines with function names (and source lines in the recompiled
game code, which map to the original functions through `src_tu2/symbols.tsv`):

```
python3 ps5/tools/ps5_symbolize.py out/ps5/ps5-link/build/llvm-pie.elf ds2_001.log
```

## Where things are

| | |
|---|---|
| `sdk/rexglue_ps5.patch` | the PS5 platform layer for ReXGlue v0.10.0, on top of `sdk/rexglue_ds2.patch` |
| `sdk/ffmpeg_ps5.patch` | FFmpeg's build configuration for the PS5 (from the GTA IV port) |
| `ps5/CMakeLists.txt`, `ps5/cmake/` | the build: SDK, recompiled game and app as one static program |
| `ps5/src/ds2_ps5_app.h` | the app: start-up settings, the missing-files screen, disc mounting |
| `ps5/src/ds2_ps5_install.*` | finding and preparing the game from the player's files |
| `ps5/src/ds2_ps5_pch.h` | the recompiled code's precompiled header with the PS5's memory offset |
| `ps5/tools/` | SDK fetch, link and packaging, log symbolizer |
| `ps5/package/` | home screen icon, `install/README.txt`, the settings template |

## What the PS5 layer does

`sdk/rexglue_ps5.patch` carries the GTA IV port's platform work over to this project's SDK
version (that port is on a ReXGlue 0.9 fork with its own renderer; this one is 0.10.0 with
the Xenos GPU emulation):

- **Guest memory** in PS5 direct memory, mirrored with fixed views (`Memory::MapViewsPs5`);
  protection changes only, never `mmap`, inside it. The PS5 has 16 KB pages: the SDK's
  host-page reconciliation (written for Apple Silicon) does the rest.
- **Disc images** read with `pread` past the file cache instead of mapped; small files read
  whole instead of mapped.
- **Threads**: stacks in direct memory, no real-time signals, no affinity masks, FreeBSD
  thread names; fibers switched by hand (`ucontext` doesn't switch on the PS5).
- **Faults**: the PS5's signal context layout, `SIGBUS` as an access violation, a crash line
  in the log.
- **Clock** on the TSC, and a guest clock that needs no lock.
- **The global lock** as a spin lock (the GTA IV port measured `std::recursive_mutex` making
  the game run slow).
- **Display** through `VK_KHR_display` on the statically linked RADV; SDL's dummy video
  driver carries the events.
- **DualSense** through `scePad`, **audio** through `sceAudioOut`.
- **One static program**: the runtime and the Xenos backend are static libraries, and the
  SDK's `XamSwapDisc` stub gives way to the app's.

## Checks

What can be checked without a console or the PS5 toolchain's own build:

```
# every SDK source in the PS5 build, and the app's own, syntax-only for x86_64-sie-ps5
```

All 243 SDK sources and the 3 app sources pass with clang 18, the payload SDK's headers and
libc++ 18 headers. That proves the code parses and type-checks for the PS5; it does not
prove it links or runs.

## Licence

The PS5 layer is derived from the GTA IV PS5 port (GPL-3.0) and uses the PS5 payload SDK's
platform library (GPL-3.0): the PS5 files here (`ps5/`, `sdk/rexglue_ps5.patch`) are under
the GPL-3.0. ReXGlue keeps its own BSD-style licence. None of these cover the game.
