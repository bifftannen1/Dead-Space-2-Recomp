// Dead Space 2 (Xbox 360) - ReXGlue recompiled project, mods build (DS2_MODS=ON).
//
// Replaces main.cpp in a mods build (app/modapi/modapi.cmake). The plain build compiles
// main.cpp and nothing from app/modapi/. Ds2ModApp adds, to Ds2App:
//   * reading <exe>/mods/*.toml and activating the hooks of enabled mods (before the guest runs);
//   * a loose-file overlay over game:/d: when an enabled mod has a files root.

#include "ds2_rex_diagnostic_init.h"

#include "ds2_app.h"
#include "ds2_mod_loader.h"

class Ds2ModApp : public Ds2App {
 public:
  using Ds2App::Ds2App;

  static std::unique_ptr<rex::ui::WindowedApp> Create(rex::ui::WindowedAppContext& ctx) {
    return std::unique_ptr<Ds2ModApp>(new Ds2ModApp(ctx, "ds2", PPCImageConfig));
  }

 protected:
  void OnLoadXexImage(std::string& xex_image) override {
    auto lr = ds2::mod::LoadMods();
    Ds2App::OnLoadXexImage(xex_image);
    if (!ds2::mod::MountOverlay(runtime()->file_system(), lr)) {
      REXLOG_ERROR("ds2-mods: failed to mount the mod file overlay");
      rex::ShowSimpleMessageBox(rex::SimpleMessageBoxType::Error,
                                "ds2-mods: failed to mount the mod file overlay (see the log)");
      rex::FlushLogging();
      std::_Exit(1);
    }
    ds2::mod::ReportConflicts();
  }
};

REX_DEFINE_APP(ds2_rex_diagnostic, Ds2ModApp::Create)
