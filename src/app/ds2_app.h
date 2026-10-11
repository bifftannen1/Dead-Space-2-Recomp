// Dead Space 2 (Xbox 360) - ReXGlue recompiled project.
//
// Built on the SDK app template (rex::ReXApp, src/ui/rex_app.cpp). The template
// already does what the diagnostic probe (tools/diagnostics/r1_4_launch_probe_v16.cpp)
// had to re-implement by hand:
//   * SDL audio            (rex_app.cpp:310; probe :1553, R1-15)
//   * default input stack  (rex_app.cpp:311; probe :1570-1572, R1-19)
//   * GPU plugin load      (rex_app.cpp:317; probe :1584)
//   * cvar::Init + ApplyEnvironment, with replay for plugin cvars registered late
//                          (windowed_app_main_sdl.cpp:40-41; probe :1442 + :1595, R1-24)
//   * persistent shader storage (rex_app.cpp:460-465; probe :1831-1845, R1-27)
//   * persistent user data (user folder / --user_data_root; probe R1_27_USER_ROOT)
//   * window close -> TerminateTitle + exit (rex_app.cpp:498-512; probe :560, :1668, R1-31)
//
// What the template does NOT do, and this file adds:
//   1. Defaults gpu_plugin=xenos and mnk_mode=true (the template's gpu_plugin default is
//      empty = no GPU; the launcher always set REX_MNK_MODE=true, r1_4_launch_v24.py:1007).
//      Command line, REX_<FLAG> env and ds2.toml still win.
//   2. Mounts the retail disc image with the SDK's DiscImageDevice at \Device\Cdrom0 and
//      points game: and d: at it (probe :1693-1712, R1-6). The template only mounts a host
//      folder.
#pragma once

#include <rex/cvar.h>
#include <rex/filesystem.h>
#include <rex/filesystem/devices/disc_image_device.h>
#include <rex/filesystem/devices/host_path_device.h>
#include <rex/filesystem/vfs.h>
#include <rex/logging.h>
#include <rex/rex_app.h>
#include <rex/runtime.h>
#include <rex/system.h>

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <filesystem>
#include <memory>
#include <string>
#include <vector>

REXCVAR_DEFINE_STRING(ds2_iso, "", "DS2",
                      "Disc 1 ISO mounted as game:/d:. Empty: the first *.iso next to the "
                      "executable or in game_data_root; with none, game_data_root is used as an "
                      "extracted disc folder.");

class Ds2App : public rex::ReXApp {
 public:
  using rex::ReXApp::ReXApp;

  static std::unique_ptr<rex::ui::WindowedApp> Create(rex::ui::WindowedAppContext& ctx) {
    return std::unique_ptr<Ds2App>(new Ds2App(ctx, "ds2", PPCImageConfig));
  }

 protected:
  // game_data_root must be a folder holding default.xex: ConstructRuntime checks
  // game_data_root/default.xex before loading (rex_app.cpp:265-284). tools/build.py stages
  // the user's XEX into <exe>/game/, which is the default here.
  void OnConfigurePaths(rex::PathConfig& paths) override {
    if (!paths.game_data_root.empty()) return;
    std::error_code ec;
    auto staged = rex::filesystem::GetExecutableFolder() / "game";
    if (std::filesystem::is_directory(staged, ec)) paths.game_data_root = staged;
  }

  // After ds2.toml is loaded and before SetupPresentation reads gpu_plugin.
  void OnPostInitLogging() override {
    DefaultFlag("gpu_plugin", "xenos");
    DefaultFlag("mnk_mode", "true");
  }

  // Runs after Runtime::Setup (VFS exists) and before LoadXexImage.
  void OnLoadXexImage(std::string& xex_image) override {
    auto iso = FindIso();
    if (iso.empty()) {
      REXLOG_INFO("ds2: no disc image; using game_data_root {} as the disc",
                  game_data_root().string());
      return;
    }
    auto* vfs = runtime()->file_system();
    constexpr const char* kCdrom = "\\Device\\Cdrom0";
    auto disc = std::make_unique<rex::filesystem::DiscImageDevice>(kCdrom, iso);
    if (!disc->Initialize() || !vfs->RegisterDevice(std::move(disc))) {
      Fail("Failed to mount disc image: " + iso.string());
    }
    // Same VFS as the probe: only the disc. SetupVfs mounted game_data_root (the staged XEX
    // folder) at Partition1 and a NullDevice at Harddisk0; the probe ran with neither.
    vfs->UnregisterSymbolicLink("game:");
    vfs->UnregisterSymbolicLink("d:");
    vfs->UnregisterDevice("\\Device\\Harddisk0\\Partition1");
    vfs->UnregisterDevice("\\Device\\Harddisk0");
    vfs->RegisterSymbolicLink("game:", kCdrom);
    vfs->RegisterSymbolicLink("d:", kCdrom);
    std::error_code ec;
    const bool tu = std::filesystem::is_regular_file(game_data_root() / "default.xexp", ec);
#if defined(DS2_TARGET_TU2)
    // TU2 build (src_tu2/): ReXGlue applies <xex>p next to the loaded XEX and the launcher requires the
    // XEX under game:, so game: is the host folder (default.xex + default.xexp) and d: stays the disc.
    if (!tu) {
      Fail("this is the TU2 build: game\\default.xexp (Title Update #2, disc001) is required next to "
           "default.xex (docs/BUILDING.md).");
    }
    constexpr const char* kTu = "\\Device\\Ds2TitleUpdate";
    auto tud = std::make_unique<rex::filesystem::HostPathDevice>(kTu, game_data_root(), true);
    if (!tud->Initialize() || !vfs->RegisterDevice(std::move(tud))) {
      Fail("Failed to mount title update folder: " + game_data_root().string());
    }
    vfs->UnregisterSymbolicLink("game:");
    vfs->RegisterSymbolicLink("game:", kTu);
    REXLOG_INFO("ds2: TU2 build: game: = {} (default.xex + default.xexp), d: = disc", game_data_root().string());
#else
    // Base build (src/): a title-update patch would be applied to the image while the recompiled code
    // stays pre-patch. Refuse it.
    if (tu) {
      Fail("game\\default.xexp found: this build is the unpatched (base) recomp and cannot run a title "
           "update. Remove default.xexp, or build the TU2 target (docs/BUILDING.md).");
    }
#endif
    REXLOG_INFO("ds2: mounted {} at {} (game:, d:); loading {}", iso.string(), kCdrom, xex_image);
  }

 private:
  static void DefaultFlag(const char* name, const char* value) {
    if (!rex::cvar::GetFlagInfo(name) || rex::cvar::HasNonDefaultValue(name)) return;
    rex::cvar::SetFlagByName(name, value);
  }

  [[noreturn]] static void Fail(const std::string& msg) {
    REXLOG_ERROR("{}", msg);
    rex::ShowSimpleMessageBox(rex::SimpleMessageBoxType::Error, msg);
    rex::FlushLogging();
    std::_Exit(1);
  }

  std::filesystem::path FindIso() const {
    std::string flag = REXCVAR_GET(ds2_iso);
    if (!flag.empty()) {
      std::filesystem::path p = flag;
      std::error_code ec;
      if (!std::filesystem::is_regular_file(p, ec)) Fail("--ds2_iso does not exist: " + flag);
      return p;
    }
    for (const auto& dir : {rex::filesystem::GetExecutableFolder(), game_data_root()}) {
      std::error_code ec;
      if (dir.empty() || !std::filesystem::is_directory(dir, ec)) continue;
      std::vector<std::filesystem::path> isos;
      for (const auto& e : std::filesystem::directory_iterator(dir, ec)) {
        auto ext = e.path().extension().string();
        std::transform(ext.begin(), ext.end(), ext.begin(),
                       [](unsigned char c) { return char(std::tolower(c)); });
        if (ext == ".iso" && e.is_regular_file(ec)) isos.push_back(e.path());
      }
      if (!isos.empty()) {
        std::sort(isos.begin(), isos.end());
        return isos.front();
      }
    }
    return {};
  }
};
