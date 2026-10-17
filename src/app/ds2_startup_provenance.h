#pragma once

// DS2-local adaptation, NO_DONOR_COPY. Query only; never apply a setting.
#include <rex/chrono/clock.h>
#include <rex/cvar.h>
#include <rex/filesystem.h>
#include <rex/logging.h>
#include "ds2_build_provenance.h"

#include <Windows.h>
#include <bcrypt.h>
#include <shellapi.h>
#include <tlhelp32.h>
#include <algorithm>
#include <array>
#include <cctype>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <string_view>
#include <vector>

namespace ds2::provenance {
// Approved S1-S6 controls plus launcher/legacy aliases. Other argv/env values
// are never printed. Source: sprint-design/DS2-SPRINT-MAP.txt and proposed TOML.
inline constexpr const char* kKeys[] = {
    "gpu_plugin", "mnk_mode", "clock_no_scaling", "clock_source_raw", "resolution_scale",
    "draw_resolution_scale_x", "draw_resolution_scale_y", "anisotropic_override",
    "present_effect", "present_dither", "swap_post_effect", "async_shader_compilation",
    "vulkan_pipeline_creation_threads", "vulkan_async_skip_incomplete_frames",
    "vulkan_submit_on_primary_buffer_end", "vulkan_dynamic_rendering", "render_target_path_vulkan",
    "vulkan_sparse_shared_memory", "direct_host_resolve", "store_shaders", "readback_resolve",
    "readback_memexport", "readback_memexport_fast", "clear_memory_page_state",
    "occlusion_query_enable", "vsync", "vulkan_allow_present_mode_immediate",
    "vulkan_allow_present_mode_mailbox", "vulkan_allow_present_mode_fifo_relaxed",
    "host_present_from_non_ui_thread", "texture_cache_memory_limit_render_to_texture",
    "texture_cache_memory_limit_soft", "texture_cache_memory_limit_hard",
    "texture_cache_memory_limit_soft_lifetime", "ignore_thread_priorities",
    "ignore_thread_affinities", "audio_maxqframes", "audio_mute", "ffmpeg_verbose",
    "log_level", "log_verbose", "log_noisy", "log_flush_interval", "ds2_mods_trace_opens",
    "gpu_debug_markers", "vulkan_validation_enabled", "vulkan_stall_timing",
    "vulkan_pipeline_timing", "p3_draw_cpu", "precompiled_shaders_path",
    "p4_wait_idle_after_pipeline", "p4_pipeline_settle_ms", "p7_nx_pipeline_warmup",
    "p4_vk_pipeline_cache_path", "vulkan_readback_resolve", "vulkan_readback_memexport",
    "video_mode_refresh_rate", "video_mode_width", "video_mode_height", "window_width",
    "window_height", "fullscreen", "ds2_mods", "ds2_mods_dir", "ds2_iso", "game_data_root",
    "user_data_root", "cache_root"};

inline bool Allowed(std::string_view key) {
  for (auto candidate : kKeys) if (key == candidate) return true;
  return false;
}
inline const char* SourceName(rex::cvar::Source source) {
  switch (source) {
    case rex::cvar::Source::kDefault: return "default";
    case rex::cvar::Source::kConfig: return "config";
    case rex::cvar::Source::kEnvironment: return "env";
    case rex::cvar::Source::kCommandLine: return "cmdline";
    case rex::cvar::Source::kRuntime: return "runtime";
  }
  return "unknown";
}
inline std::string Quote(std::string_view value) {
  constexpr char hex[] = "0123456789abcdef";
  std::string out = "\"";
  for (unsigned char c : value) {
    if (c == '"' || c == '\\') { out += '\\'; out += char(c); }
    else if (c < 0x20 || c == 0x7f) {
      out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15];
    } else out += char(c);
  }
  return out + '"';
}
inline std::string Utf8(const wchar_t* value) {
  const int n = WideCharToMultiByte(CP_UTF8, 0, value, -1, nullptr, 0, nullptr, nullptr);
  if (n <= 0) return {};
  std::string out(size_t(n), '\0');
  WideCharToMultiByte(CP_UTF8, 0, value, -1, out.data(), n, nullptr, nullptr);
  out.pop_back();
  return out;
}
inline std::string Sha256(const std::filesystem::path& path) {
  std::ifstream file(path, std::ios::binary);
  if (!file) return "unreadable";
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_size = 0, returned = 0;
  if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0)
    return "hash_error";
  bool ok = BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
      reinterpret_cast<PUCHAR>(&object_size), sizeof(object_size), &returned, 0) >= 0;
  std::vector<unsigned char> object(object_size);
  if (ok) ok = BCryptCreateHash(algorithm, &hash, object.data(), object_size, nullptr, 0, 0) >= 0;
  std::array<char, 65536> buffer{};
  while (ok && file) {
    file.read(buffer.data(), buffer.size());
    if (file.gcount()) ok = BCryptHashData(hash, reinterpret_cast<PUCHAR>(buffer.data()),
                                          ULONG(file.gcount()), 0) >= 0;
  }
  std::array<unsigned char, 32> digest{};
  if (ok) ok = !file.bad() && BCryptFinishHash(hash, digest.data(), digest.size(), 0) >= 0;
  if (hash) BCryptDestroyHash(hash);
  BCryptCloseAlgorithmProvider(algorithm, 0);
  if (!ok) return "hash_error";
  constexpr char hex[] = "0123456789abcdef";
  std::string out;
  for (auto c : digest) { out += hex[c >> 4]; out += hex[c & 15]; }
  return out;
}
inline void LogFlag(std::string_view phase, const char* name) {
  if (!rex::cvar::GetFlagInfo(name)) {
    REXLOG_INFO("DS2_SETTING phase={} key={} registered=false", phase, name);
    return;
  }
  REXLOG_INFO("DS2_SETTING phase={} key={} registered=true value={} source={}", phase, name,
              Quote(rex::cvar::GetFlagByName(name)), SourceName(rex::cvar::GetFlagSource(name)));
}
inline void Dump(bool p4_seeded) {
  REXLOG_INFO("DS2_STARTUP begin boundary=OnLoadXexImage after_plugin_registration=true");
  REXLOG_INFO("DS2_STARTUP build_sources_sha256={} scope=scoped_inputs_not_full_tree",
              Quote(kBuildSources));
  auto exe = rex::filesystem::GetExecutablePath();
  REXLOG_INFO("DS2_IDENTITY role=exe path={} sha256={}", Quote(exe.string()), Sha256(exe));
  // Inspect loaded modules rather than assuming the Release DLL naming variant.
  HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, GetCurrentProcessId());
  if (snapshot != INVALID_HANDLE_VALUE) {
    MODULEENTRY32W module{}; module.dwSize = sizeof(module);
    if (Module32FirstW(snapshot, &module)) do {
      std::string name = Utf8(module.szModule);
      std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return char(std::tolower(c)); });
      if (name.starts_with("rex") && name.ends_with(".dll")) {
        auto path = std::filesystem::path(module.szExePath);
        REXLOG_INFO("DS2_IDENTITY role=loaded_dll path={} sha256={}", Quote(path.string()), Sha256(path));
      }
    } while (Module32NextW(snapshot, &module));
    CloseHandle(snapshot);
  } else REXLOG_INFO("DS2_IDENTITY role=loaded_dll status=unavailable");
  auto registered = rex::cvar::ListFlags();
  REXLOG_INFO("DS2_STARTUP registered_count={} planned_count={}", registered.size(), std::size(kKeys));
  for (auto key : kKeys) {
    LogFlag("effective", key);
    std::string env = "REX_";
    for (unsigned char c : std::string_view(key)) env += char(std::toupper(c));
    if (const char* value = std::getenv(env.c_str()))
      REXLOG_INFO("DS2_ENV key={} value={} origin={}", env, Quote(value),
          env == "REX_P4_VK_PIPELINE_CACHE_PATH" && p4_seeded ? "ds2_seeded" : "user_env");
  }
  int argc = 0, omitted = 0;
  wchar_t** argv = CommandLineToArgvW(GetCommandLineW(), &argc);
  if (argv) {
    for (int i = 1; i < argc; ++i) {
      auto arg = Utf8(argv[i]);
      const auto split = arg.find('=');
      if (arg.starts_with("--") && Allowed(std::string_view(arg).substr(2, split == std::string::npos ? split : split - 2))) {
        if (split != std::string::npos) REXLOG_INFO("DS2_ARG index={} value={}", i, Quote(arg));
        else if (i + 1 < argc && !std::wstring_view(argv[i + 1]).starts_with(L"--")) {
          REXLOG_INFO("DS2_ARG index={} value={}", i, Quote(arg + "=" + Utf8(argv[i + 1]))); ++i;
        } else REXLOG_INFO("DS2_ARG index={} value={}", i, Quote(arg));
      } else ++omitted;
    }
    LocalFree(argv);
  }
  REXLOG_INFO("DS2_STARTUP argv_omitted={} argv_status={}", omitted, argv ? "parsed" : "unavailable");
  REXLOG_INFO("DS2_STARTUP guest_scalar={} scalar_source=Clock::guest_time_scalar",
              rex::chrono::Clock::guest_time_scalar());
  auto mods_dir = rex::cvar::GetFlagByName("ds2_mods_dir");
  auto root = mods_dir.empty() ? exe.parent_path() / "mods" : std::filesystem::path(mods_dir);
  std::error_code ec;
  std::vector<std::filesystem::path> manifests;
  for (std::filesystem::directory_iterator it(root, ec), end; !ec && it != end; it.increment(ec))
    if (it->path().extension() == ".toml" && it->is_regular_file(ec)) manifests.push_back(it->path());
  std::sort(manifests.begin(), manifests.end());
  for (const auto& path : manifests)
    REXLOG_INFO("DS2_MOD_MANIFEST path={} sha256={} status=present_not_activation_proof",
                Quote(path.string()), Sha256(path));
  REXLOG_INFO("DS2_STARTUP mod_manifest_count={} enumeration_error={}", manifests.size(), ec.value());
  using GetHint = const char* (__cdecl*)(const char*);
  auto sdl = GetModuleHandleW(L"SDL3.dll");
  auto get_hint = sdl ? reinterpret_cast<GetHint>(GetProcAddress(sdl, "SDL_GetHint")) : nullptr;
  const char* hint = get_hint ? get_hint("SDL_TIMER_RESOLUTION") : nullptr;
  REXLOG_INFO("DS2_STARTUP SDL_TIMER_RESOLUTION={} query_status={}",
              Quote(hint ? hint : "unset"), get_hint ? "queried" : "unavailable");
  REXLOG_INFO("DS2_STARTUP presentation_identity_source=GPU_STARTUP_GPU_SURFACE_WINDOW_STARTUP");
  REXLOG_INFO("DS2_STARTUP end runtime_qualification=HOLD_NOT_MEASURED");
}
}  // namespace ds2::provenance
