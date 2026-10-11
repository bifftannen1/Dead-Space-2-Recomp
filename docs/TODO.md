# Roadmap

## Multi-disc: swap or no swap (open)
The prompt "insert disc / please insert 'disc N' to continue" is
`EARS::Modules::MultiDiscManager::OnEnterStateWaitingForDisc` (0x8280F190, DeadSpace/src/modules/multidisc/multidisc.cpp):
`DetermineDisc` reads `discN_info.txt`, `GetDiscNumberForChapter` disagrees, the popup is shown, the bigfiles are
removed and `XSwapDisc` -> `XamSwapDisc` waits for the new disc. ReXGlue's `XamSwapDisc`
(src/kernel/xam/xam_info.cpp:325) is a stub that returns success without changing `d:`, so the game waits forever.

Goal (preferred): no swap at all when both ISOs are in the folder.
- Detect `... (Disc 1).iso` and `... (Disc 2).iso` (or `ds2_iso` + `ds2_iso_disc2`).
- Implement `XamSwapDisc(n)`: remount `\Device\Cdrom0` (`d:`) onto disc n's ISO and signal the waiting event
  immediately, so `MultiDiscManager` re-reads `discN_info.txt` and `AddBigFiles` mounts the new bigfiles; the popup
  closes on the next tick. With both ISOs present the player never sees the prompt.
- Fallback with one ISO: keep the prompt and let the user pick the other ISO (file dialog / hotkey).
- Both discs patch to identical images under TU2 (all 8 sections byte-equal); only the XEX headers differ, so one
  recomp serves both discs. Test with a save past the disc split once swap works.

## Requested features (from comments, 2026-10-11), in priority order
Hooks are named from the PDB tree; all of these should be mods or runtime options, never edits to generated code.

1. **Raw mouse input.** The game reads only the pad (XInput through the runtime's input system). Add a mouse
   path: raw/relative mouse in the host (SDL relative mode) feeding camera yaw/pitch directly, not
   mapped onto a fake right stick (avoids stick acceleration curves and dead zones). Starting points: the
   player camera's input handling in the named tree, and the runtime input driver (`src/input/`).
2. **Ultrawide.** `EARS::Framework::CameraManager::AdaptFOVtoAspectRatio` (0x8250EC38),
   `GetActiveCamAuthoredAspectRatio` (0x8250F488), `RenderContext::RC_RebuildAndApplyProjectionMatrix`
   (0x8250E330; the `fov` mod already hooks it). Needs Hor+ FOV, a non-16:9 back buffer from the runtime
   and HUD/menu anchoring (Apt UI) so the HUD isn't stretched.
3. **Above 60 FPS.** 60 works with the `framerate` mod: the game clock target (`g_targetFrameTimeMSec` = 2 x ONE_FRAMETIME, 1/59.94 s) and the frame lock (`Displ_SwapScreens` -> `Displ_WaitForFrameLock(2)`) both pinned the game to 30. Heavy scenes dip to ~50 on the emulated Xenos path; a native renderer (as Hell's Gate did for Dante's Inferno, same engine) is the route past 60.
   Original notes: `EARS::Time::SetTargetFrameRateFields` (0x82522438),
   `D3DDevice_SetRenderState_PresentInterval` (0x8221B0A8), runtime `vsync` cvar. Risk: gameplay, physics
   (Havok step) and animation may assume a fixed 30/60 Hz step; measure first (frame-time trace,
   physics/animation at 30 vs 60 vs 120) and gate on no gameplay-speed change.
4. **Graphics options: shadow map resolution and more** (the "cherry on top").
   `EARS::ShadowHelper::RenderGeoIntoShadowMap` / `RenderStaticShadowMap` (0x8251F020 / 0x8251F370),
   `SM_Deferred*ShadowMap*` setters (0x825745E8 and siblings). Shadow-map size is a render-target allocation
   the hook would enlarge; Xenos EDRAM/render-target emulation must allow the larger target. The runtime's
   `resolution_scale` / `draw_resolution_scale_x|y` already scale rendering in general.
5. **Multiplayer.** The game's online stack is EA Blaze (BlazeSDK 3.05) on retired EA servers, plus Xbox
   Live (XNet/XSession, stubbed in ReXGlue). Needs a replacement Blaze/session server or an emulated one, and
   real XNet/QoS in the runtime. Largest item; after 1-4.
