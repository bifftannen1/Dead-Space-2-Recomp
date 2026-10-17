# Launcher audit — 2026-10-13

PASS: DS2-themed launcher and optional Disc 2 startup selection built and packaged.
HOLD: automatic in-game swaps, full campaign completion and performance claims.

## Reproduced findings and changes

| Finding | Before | After / verification |
|---|---|---|
| Missing Disc 2 | Only Disc 1 could be selected or staged | Optional Disc 2 field; selector appears when supplied; exact XEX and disc002 patch hashes; distinct staging directories; actual Disc 2 startup rendered |
| Generic interface | Stock light Tk widgets | Original dark RIG-style vector header, cyan/amber palette, themed controls and icon; real window inspected |
| Incomplete runtime manifests accepted | Empty hash dictionary returned success | All three named runtime files required; malformed hashes and tampered binaries rejected |
| Unsafe Windows filenames accepted | CON, trailing dot and question mark reproduced as accepted | Reserved names, forbidden characters, trailing whitespace/dots, duplicate names and cyclic parents refused |
| Partial staging possible | Wrote two files directly into final directory | Prepare both files in an owned temporary directory, then rename; JSON receipts/settings use atomic replacement |
| DLC reinstall unnecessarily failed | Any existing destination rejected | Existing bytes compared with the selected package; 421 real Severed files verified without overwriting |
| Truncated package handling | Low-level unpack errors and unbounded malformed block counts | Header/table/block bounds and block-chain cycle checks; malformed ZIP candidates skipped |
| Concurrent UI actions | Controls remained editable; launch became available while game ran | Disable controls during work and tracked game lifetime; closing during installation is blocked |
| Window placement | Cascaded window could put launch buttons below screen edge | Explicit initial position and reduced height; final executable rechecked |

## Disc 2 causal chain and rejected explanations

1. Hypothesis: Disc 2 was supported but hidden by the UI. Rejected for the old launcher: `prepare` and `command` always staged and mounted Disc 1.
2. Hypothesis: a second ISO argument alone enables swaps. Rejected: SDK `src/kernel/xam/xam_info.cpp:325` returns success from `XamSwapDisc` without remounting. No invented `ds2_iso_disc2` argument was added.
3. Hypothesis: the existing TU2 executable can start with Disc 2's paired XEX/patch and mounted ISO. Supported by `docs/TU2.md`, then directly tested with the measured Disc 2 artifacts and actual renderer.

Implemented chain: optional ISO -> exact executable hash -> matching `disc002/default.xexp` hash -> separate staged game directory -> selected ISO through supported `ds2_iso` -> selected game directory through `game_data_root` -> unchanged TU2 executable/runtime -> captured title screen and subsequent rendered startup frames.

## Evidence and boundary

- 14/14 launcher regression tests passed (`python launcher/test_launcher.py`).
- Both real TU2 patches matched their expected hashes; wrong Media ID rejection remains tested by `tools/verify_launcher_inputs.py`.
- Disc 2 archive extraction completed with 7-Zip `Everything is Ok`; ISO size 7,838,695,424 bytes. The XEX is title 454108DF, Media ID 403243EC, disc 2/2, SHA-256 `78d834922b39a68de45b0da319b9d90d28d62d7db1d12e0c8446dbf302c18a6e`.
- Actual frozen installer passed with both discs selected and active disc 2. Receipt confirms Disc 2 XEX/patch/ISO association.
- `smoke-audit-disc2/smoke.json`: frames at 20, 45 and 70 seconds; alive at final capture; clean close, exit 0, no remaining game process. Title-screen and 70-second pixels inspected. This is startup/render proof, not campaign or frame-rate proof.
- `launcher/package-audit.json`: 8/8 package checks passed, including CRC, three binary hashes, no user game inputs/state, and unchanged game/runtime bytes versus v1.
- Initial v1 ZIP retained at `dist/DS2-TU2-Alpha-v1.zip`.

Disc 1 remains required; Disc 2 and Severed are optional. Select a disc before launching. The UI and README state that automatic in-game disc swaps are unsupported. No shared runtime or generated game source was modified in this audit.
