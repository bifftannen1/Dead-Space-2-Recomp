# Building the alpha launcher

Use the accompanying DS2 TU2 source checkout and its pinned SDK patch. The game build is `DS2_TARGET=tu2`; the existing base executable must not be relabeled as TU2.

1. Build and install the SDK using `tools/build.py`'s pinned revision, patch and options.
2. Run `python tools/build_tu2_60.py --evidence-dir research/launcher-options/build`. It uses that SDK, TU2, `DS2_MODS=ON`, `DS2_OPT=-O2`, and the isolated `profiles/claude60/mods` sources. The generated graph must contain both framerate hooks, the FOV projection hook and the disc adapter, with no native renderer. Build input hashes are recorded before compilation and checked afterward.
3. Install PyInstaller in your build environment, then run from the repository root:

```
python tools/build_launcher.py
python launcher/test_launcher.py
python tools/qualify_launcher_options.py
python tools/package_launcher.py
```

The package script expects the SDK installed at `sdk-build-source/out/install/ds2-release` and checks the TU2/mods CMake cache. Before packaging, use `tools/run_settings_probe.py` for the three named runs in `tools/qualify_launcher_options.py`; complete Disc 2 to New Game in the 30 FPS run. Qualification checks all three actual hook settings and the ordered swap log. Review the timing separately: a measured sample is not a guarantee of sustained 60 FPS. The package checks the tested EXE/DLL hashes, source hashes and frozen launcher receipt. It packages only an explicit file allowlist, including the recovered mods, dependency notices and launcher source. Game content, local settings, caches and logs are excluded. `DS2-TU2-Alpha.zip.sha256` records the final ZIP hash.

The launcher supports a non-GUI validation path:

```
DS2Launcher.exe --verify --disc1 "Disc 1.iso" --update "DeadSpace2_TU2.zip" --user "test-user"
```

Use the process exit code and `test-user/launcher-receipt.json` with a windowed executable. This verifies installation, not game rendering.

TU2 builds compile `src/app/ds2_disc_swap.cpp`. The launcher passes both verified ISO paths to `ds2_disc1_iso` and `ds2_disc2_iso`; `ds2_iso` selects the initial disc. Keep the app source with the packaged executable; the SDK DLLs are unchanged.

Optional Disc 2: append `--disc2 "Disc 2.iso" --active-disc 2`. The same TU ZIP/package must contain both disc patches. The GUI offers the selector only when a Disc 2 path is present; validation still checks its exact XEX and patch hashes before launch.
