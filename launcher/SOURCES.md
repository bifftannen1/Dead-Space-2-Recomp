# Launcher contract — 2026-10-13

Target: supplied `Dead-Space-2-Recomp-main (2).zip`, TU2 CMake target.
The DS2 app layer supplies automatic disc swapping. Lifted guest bodies and the shared SDK are unchanged.

Source-backed interfaces:

- `tools/build.py:31` and `:174`: Disc 1 XEX and TU2 Disc 1 XEXP SHA-256 pins.
- `tools/extract_xex.py:20-59`: XDVDFS volume offsets and directory walk reused directly.
- `tools/extract_dlc.py:27-77`: STFS metadata, table, and block readers reused directly.
- `tools/extract_dlc.py:105`: runtime DLC content-directory layout.
- `src/app/ds2_app.h`: `ds2_iso` boot selection; `src/app/ds2_disc_swap.cpp`: TU2 mount preservation and `ds2_disc1_iso` / `ds2_disc2_iso` availability.
- ReXGlue SDK `c94f5ebdcb3c9d1a460ca48e04f9758448f8d518`, `src/ui/windowed_app_main_sdl.cpp:40-41`: cvar argument/environment initialization.
- SDK `src/system/runtime.cpp:31-34`: game/user/cache directory overrides.
- SDK `src/ui/window.cpp:37`: fullscreen; `src/input/mnk/mnk_input_driver.cpp:26`: keyboard/mouse emulation.
- SDK `src/ui/rex_app.cpp:412-416`: F4 settings overlay.
- SDK `src/kernel/xam/xam_info.cpp:325` remains a stub. The TU2 app provides a strong local XAM import and dispatcher registration, persistent ISO devices, an atomic alias transaction and completion event signaling only after success.
- Design reference: Infinite Undiscovery `614a0a0ebd72103fce8aaa52c715d847b703a53e`, `src/disc_swap.h`. DS2 preserves `game:` for its title update while selecting `d:` and the CD-ROM alias. Its generated XSwapDisc wrapper supplies a KEVENT pointer to XAM, not an event handle. The reference license accompanies the package.
- `tools/inspect_disc2_archive.py` reads the actual Disc 2 XEX through the same XDVDFS parser. `test-inputs/disc2/default.json` records its SHA-256, 14,069,760-byte size, title 454108DF, Media ID 403243EC and disc number 2/2. `launcher/disc2_identity.py` pins that measured XEX hash.
- TU package for Media ID 2915C8F8, `disc002/default.xexp`: 1,288,192 bytes, SHA-256 `dd5aaf4edba09cc332c75bb2027bfe308a077c52c411df2070fdf3193372fb5e`, extracted with the repository STFS reader. The two discs get separate content-addressed staging folders.

The installer requires the user's local ISO and update. It can locate the matching patch inside a TU ZIP with multiple Media IDs by exact payload hash. It never downloads game data. DLC packages must have title ID 454108DF and content type 00000002. That check proves DS2 DLC membership, not a specific Severed release hash; the selected package display name is recorded in the installation receipt.

The package manifest must enumerate and match all three runtime binaries. This is integrity/provenance evidence, not proof of playable game behavior. Installation receipts stay in the chosen user-data folder and are replaced atomically. Staged XEX/patch directories are published only when complete. Existing differing staged files are refused. Re-selecting an installed DLC verifies its bytes and succeeds without rewriting it; differing contents are refused.

The RIG-style UI uses original vector lines, typography and a cyan/amber palette. It contains no extracted game artwork. The file controls and launch button are disabled during work and while its game process runs. Closing during installation is blocked until the operation finishes.

Options use `settings_model.py` as a closed enum contract. `tools/audit_launcher_settings.py`
checks 120 combinations, rejects invalid values and altered profiles, locates every
argument's CVar declaration, and checks strings in the actual built EXE/DLLs.
These checks establish wiring, not gameplay behavior. `tools/audit_live_settings.py`
separately freezes and checks actual hook logs and records swapchain sizes and timing.
Window dimensions come from SDK `src/ui/window.cpp` and `src/ui/window_sdl.cpp`;
they are logical window dimensions, not internal renderer resolution. Fullscreen uses
the desktop. Framerate uses the upstream two-hook mod with vblanks 2 or 1.
FOV uses the upstream before/after projection hook at TU2 0x8250E330, resolved in
`tools/modapi/symbols_tu2.tsv:8338`; render-context offsets 416/428/432 also occur in
`src_tu2/EARS/ears_framework/src/framework/core/graphics/rendercontext.cpp`.
Original FOV disables the mod. Enabled presets scale by 1.10 or 1.25 and restore
the original context value after projection. Native renderer sources are excluded.
