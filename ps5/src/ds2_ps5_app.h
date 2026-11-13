// Dead Space 2 for PS5 - the app.
//
// The PC app (src/app/ds2_app.h) with what a console needs instead of a
// launcher: the game's files are found and prepared at start-up
// (ds2_ps5_install.h), everything lives in the app's own folder (/app0, which
// is /data/homebrew/<title id>), and a screen says what to do when the files
// aren't there. The disc swap (src/app/ds2_disc_swap.cpp) is the PC's, as it is.
#pragma once

#include <rex/cvar.h>
#include <rex/filesystem.h>
#include <rex/logging.h>
#include <rex/rex_app.h>
#include <rex/runtime.h>
#include <rex/system.h>
#include <rex/ui/imgui_dialog.h>

#include <imgui.h>

#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <functional>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>

#include <unistd.h>

#include "ds2_disc_swap.h"
#include "ds2_ps5_install.h"

// The game's language: the Xbox 360's, taken from the PS5's unless set here.
// The World (En,Fr,Es) discs have English and French voices, and Spanish text
// with English voices.
REXCVAR_DEFINE_STRING(ps5_language, "auto", "PS5",
                      "Game language: auto (the PS5's), english, french or spanish");

extern "C" int sceSystemServiceParamGetInt(int param_id, int* value);
extern "C" int sceKernelSendNotificationRequest(int, void*, size_t, int);

namespace ds2ps5 {

inline void Notify(const std::string& text) {
  struct {
    char unused[45];
    char message[3075];
  } request;
  std::memset(&request, 0, sizeof(request));
  std::strncpy(request.message, text.c_str(), sizeof(request.message) - 1);
  sceKernelSendNotificationRequest(0, &request, sizeof(request), 0);
}

// The Xbox 360 language id (XLanguage) for the ps5_language setting.
inline uint32_t XboxLanguage(const std::string& setting) {
  constexpr uint32_t kEnglish = 1, kFrench = 4, kSpanish = 5;
  if (setting == "english") return kEnglish;
  if (setting == "french") return kFrench;
  if (setting == "spanish") return kSpanish;
  int ps5 = 1;
  if (sceSystemServiceParamGetInt(1 /* SCE_SYSTEM_SERVICE_PARAM_ID_LANG */, &ps5) != 0) ps5 = 1;
  switch (ps5) {
    case 2:   // French
    case 22:  // French (Canada)
      return kFrench;
    case 3:   // Spanish
    case 20:  // Spanish (Latin America)
      return kSpanish;
    default:
      return kEnglish;
  }
}

// A screen of text that stays up: what is missing and where to put it. There
// is no game to run behind it; the PS button closes the app.
class MessageScreen final : public rex::ui::ImGuiDialog {
 public:
  MessageScreen(rex::ui::ImGuiDrawer* drawer, std::string title, std::string body)
      : rex::ui::ImGuiDialog(drawer), title_(std::move(title)), body_(std::move(body)) {}

 protected:
  void OnDraw(ImGuiIO& io) override {
    ImGui::SetNextWindowPos(ImVec2(0, 0));
    ImGui::SetNextWindowSize(io.DisplaySize);
    ImGui::Begin("##ds2ps5-message", nullptr,
                 ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoSavedSettings |
                     ImGuiWindowFlags_NoNav);
    // The built-in type is made for a desktop window: scaled up to read from a sofa.
    const float scale = io.DisplaySize.y > 0 ? io.DisplaySize.y / 540.0f : 2.0f;
    const float margin = io.DisplaySize.x * 0.08f;
    ImGui::SetWindowFontScale(scale * 1.4f);
    ImGui::SetCursorPos(ImVec2(margin, io.DisplaySize.y * 0.12f));
    ImGui::TextUnformatted(title_.c_str());
    ImGui::SetWindowFontScale(scale);
    ImGui::SetCursorPos(ImVec2(margin, io.DisplaySize.y * 0.24f));
    ImGui::PushTextWrapPos(io.DisplaySize.x - margin);
    ImGui::TextUnformatted(body_.c_str());
    ImGui::PopTextWrapPos();
    ImGui::End();
  }

 private:
  std::string title_, body_;
};

}  // namespace ds2ps5

class Ds2Ps5App : public rex::ReXApp {
 public:
  using rex::ReXApp::ReXApp;

  static std::unique_ptr<rex::ui::WindowedApp> Create(rex::ui::WindowedAppContext& ctx) {
    return std::unique_ptr<Ds2Ps5App>(new Ds2Ps5App(ctx, "ds2", PPCImageConfig));
  }

 protected:
  // After ds2.toml is loaded and before the GPU backend is created. On the PS5
  // the backend is part of the program, so its flags exist already and take
  // their defaults here (the PC seeds one of them through the environment).
  void OnPostInitLogging() override {
    DefaultFlag("gpu_plugin", "xenos");
    // No keyboard or mouse to play with.
    DefaultFlag("mnk_mode", "false");
    // V-Synced presentation: the swapchain otherwise prefers IMMEDIATE, which tears.
    DefaultFlag("vulkan_allow_present_mode_immediate", "false");
    // As the PC launcher starts the game (launcher/launcher.py): the guest
    // clock straight from the host's, render-to-texture readback off.
    DefaultFlag("clock_no_scaling", "true");
    DefaultFlag("readback_resolve", "none");
    // One ordinary buffer for the console's 512 MB instead of a sparse one:
    // the PS5's driver is a port and has memory to spare.
    DefaultFlag("vulkan_sparse_shared_memory", "false");
    // Compiled pipelines kept between runs, so a shader only stutters the
    // first time it is seen (sdk/rexglue_ds2.patch, P4).
    if (!cache_root().empty()) {
      std::error_code ec;
      std::filesystem::create_directories(cache_root(), ec);
      DefaultFlag("p4_vk_pipeline_cache_path", (cache_root() / "vk_pipelines.bin").string().c_str());
    }
    DefaultFlag("user_language", std::to_string(ds2ps5::XboxLanguage(REXCVAR_GET(ps5_language))).c_str());
    REXLOG_INFO("Dead Space 2 for PS5 {}: language {} ({}), config {}", DS2_PS5_VERSION,
                rex::cvar::GetFlagByName("user_language"), REXCVAR_GET(ps5_language),
                (app_root() / "ds2.toml").string());
  }

  // The window and the overlay drawer exist; the runtime doesn't yet. The
  // game's files are looked for and prepared here; without them the app stays
  // on a screen that says what to copy where.
  std::optional<rex::PathConfig> OnFinalizePaths(const rex::PathConfig& defaults,
                                                 std::function<void(rex::PathConfig)> resume) override {
    (void)resume;
    files_ = ds2ps5::PrepareGameFiles(app_root(), defaults.user_data_root);
    for (const std::string& note : files_.notes) REXLOG_INFO("ds2 files: {}", note);
    const int boot = files_.boot_disc();
    if (!boot) {
      REXLOG_ERROR("ds2 files: {}", files_.problem);
      rex::FlushLogging();
      ds2ps5::Notify("Dead Space 2: game files are missing - see the screen");
      if (imgui_drawer()) {
        new ds2ps5::MessageScreen(imgui_drawer(), "Dead Space 2 needs your game files", files_.problem);
        return std::nullopt;  // (never resumed: the screen stays until the app is closed)
      }
      Fail(files_.problem);
    }
    rex::PathConfig paths = defaults;
    paths.game_data_root = files_.game_dir[boot - 1];
    return paths;
  }

  // Runs after Runtime::Setup (the VFS exists) and before LoadXexImage: the
  // disc image as d: and the title update folder as game:, both discs mounted
  // for the automatic swap when both are there (src/app/ds2_disc_swap.cpp).
  void OnLoadXexImage(std::string& xex_image) override {
    const int boot = files_.boot_disc();
    if (!boot) Fail("No Dead Space 2 disc image is ready");
    if (!files_.iso[0].empty()) rex::cvar::SetFlagByName("ds2_disc1_iso", files_.iso[0].string());
    if (!files_.iso[1].empty()) rex::cvar::SetFlagByName("ds2_disc2_iso", files_.iso[1].string());
    try {
      ds2::disc_swap::Prepare(*runtime(), files_.iso[boot - 1], game_data_root());
    } catch (const std::exception& error) {
      Fail(error.what());
    }
    REXLOG_INFO("ds2: Disc {} ({}) with Title Update #2 from {}; loading {}", boot,
                files_.iso[boot - 1].string(), game_data_root().string(), xex_image);
  }

  void OnPreLaunchModule() override {
    try {
      ds2::disc_swap::RegisterImport(*runtime());
    } catch (const std::exception& error) {
      Fail(error.what());
    }
  }

 private:
  ds2ps5::GameFiles files_;

  static std::filesystem::path app_root() { return rex::filesystem::GetExecutableFolder(); }

  static void DefaultFlag(const char* name, const char* value) {
    if (!rex::cvar::GetFlagInfo(name) || rex::cvar::GetFlagSource(name) != rex::cvar::Source::kDefault) return;
    rex::cvar::SetFlagByName(name, value);
  }

  [[noreturn]] static void Fail(const std::string& message) {
    REXLOG_ERROR("{}", message);
    rex::FlushLogging();
    ds2ps5::Notify("Dead Space 2: " + message);
    sleep(3);  // (let the notification show)
    std::_Exit(1);
  }
};
