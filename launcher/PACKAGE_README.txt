=====================================================================
 DEAD SPACE 2 (Xbox 360, Title Update 2) - PC RECOMPILATION
 *** ALPHA / WORK IN PROGRESS - PLAYTEST BUILD ***
=====================================================================

PLEASE READ FIRST
  This is an ALPHA, work-in-progress build. The game as a whole has NOT
  been fully tested. Expect bugs, visual glitches and possible crashes.
  If you hit one, please report it (see REPORTING BUGS below).

  - Framerate should be relatively stable, but there are some slight
    hitches, especially in effects-heavy scenes.
  - 60 FPS mode may cause some cutscene animation quirks. Choose 30 FPS
    on the Options tab for the original timing. Known case (logged as a
    TODO): an animation quirk in Severed after the elevator sequence.
  - FOV above 1.00x will break some lighting animations. Leave FOV on
    "Original (1.00x)" for the most correct visuals.
  - Multiplayer and Xbox LIVE features are NOT activated or implemented.

---------------------------------------------------------------------
WHAT YOU NEED  (not included - bring your own)
  Required  Dead Space 2 (World) (En,Fr,Es) (Disc 1)    .iso or .zip
  Required  Title Update 2 - "Dead Space 2 (World) (v2).zip"
            (v1 will be rejected)
  Optional  Dead Space 2 (World) (En,Fr,Es) (Disc 2)    .iso or .zip
  Optional  Dead Space 2 - Severed (World) (DLC)        .zip
  Windows 10/11 64-bit and a Vulkan-capable graphics driver.
  Zips can be selected exactly as downloaded - no need to extract.
  A zipped disc is unpacked once into your saves folder (~7.5 GB each).

---------------------------------------------------------------------
HOW TO PLAY - follow the launcher
  1. Extract this whole ZIP to a normal writable folder
     (e.g. Documents\DS2), not Program Files.
  2. Run DS2Launcher.exe.
  3. On "01 / GAME FILES", select your Disc 1, Title Update 2 and any
     optional files. The RIG health spine on the left fills as you add
     them (amber = a required file is still missing; hover for a list).
  4. Press VERIFY FILES and wait for "READY / Content verified".
  5. Press LAUNCH.
  Severed installs on the first verify/launch (about a minute); its row
  then shows INSTALLED. Discs swap automatically when the game asks.

---------------------------------------------------------------------
IN-GAME KEYS
  F10   Exit the game
  F4    ReXGlue settings overlay
  Keyboard/mouse emulation is on by default; a controller also works.

OPTIONS TAB  (applies on the next launch)
  Frame rate   60 FPS (experimental, default) or the original 30 FPS
  Window size  Windowed mode only. Fullscreen (default) always uses your
               desktop resolution.
  FOV          Original (1.00x), Wider (1.10x), Wide (1.25x)
  Resolution note: the game renders at the Xbox 360's native 1280x720
  and is upscaled to your window or screen. It is not rendered at
  1080p or higher in this build.

---------------------------------------------------------------------
REPORTING BUGS
  Send the newest file from the logs\ folder next to ds2.exe, plus a
  short note of what you were doing, which disc/chapter, and your GPU.

WHAT IS IN THIS ZIP
  DS2Launcher.exe, ds2.exe, rexruntime.dll, rexgpu-xenos.dll,
  runtime-manifest.json (file hashes, checked at every launch),
  mods\ (60 FPS and FOV), licenses\, and source for the launcher and
  app glue. No game data, ISO, XEX, title update or DLC is included.

Built on ReXGlue (Vulkan backend). Thanks to the ReXGlue and Xenia
developers. Not affiliated with or endorsed by EA or Motive.
