// Frame-rate mod: changes the engine's target frame time.
//
// EARS::Time keeps g_targetFrameTimeMSec = vblanks * ONE_FRAMETIME (1 / 59.94 s); the game sets
// vblanks = 2 (SetTargetFrameRateFields(2) in LevelServices), so it targets 30 FPS.
// TimeManager::HandleEvents advances the game clock in whole vblanks and never by less than the
// target. SetTargetFrameRateFields is not in the admitted hook-name table, so a BEFORE hook on
// HandleEvents rewrites the target each frame to settings.target_vblanks * one vblank.
// The frame itself is paced separately: Displ_SwapScreens calls Displ_WaitForFrameLock(n),
// which spins until n vblanks have passed since the last frame, with n from a per-mode table
// (2 in gameplay). A second BEFORE hook caps n at target_vblanks.
#include "ds2_modapi.h"

#include <rex/logging.h>
#include "../../../../src/app/ds2_cadence_window.h"

#include <bit>
#include <chrono>
#include <cstring>

namespace {
// ONE_FRAMETIME source and g_targetFrameTimeMSec per build (from the EARS::Time dynamic
// initialisers). The build is picked at run time: the one whose vblank value is ~16.68 ms.
struct TimeAddrs {
  uint32_t one_vblank_ms, target_frame_ms;
};
constexpr TimeAddrs kBuilds[] = {{0x82D1B960, 0x82D1F740},   // retail
                                 {0x82D437E0, 0x82D475C0}};  // Title Update #2
const TimeAddrs* FindBuild(ds2::mod::Call& c) {
  for (const auto& b : kBuilds) {
    const float v = c.load_f32(b.one_vblank_ms);
    if (v > 16.0f && v < 17.5f) return &b;
  }
  return nullptr;
}

// settings.log_fps: every 10 s, log the host frame rate seen by this once-per-frame hook.
void LogFps(double deadline_ms) {
  using clock = std::chrono::steady_clock;
  static clock::time_point last{}, window{};
  static int frames = 0, over_40 = 0, over_100 = 0;
  static ds2::provenance::CadenceWindow<> cadence;
  const auto now = clock::now();
  if (last.time_since_epoch().count()) {
    const double ms = std::chrono::duration<double, std::milli>(now - last).count();
    cadence.Add(ms, deadline_ms);
    ++frames;
    over_40 += ms > 40.0;
    over_100 += ms > 100.0;
  } else {
    window = now;
  }
  last = now;
  const double span = std::chrono::duration<double>(now - window).count();
  if (span >= 10.0) {
    REXLOG_INFO("framerate-mod: {:.1f} FPS over {:.1f} s, frames >40 ms: {}, >100 ms: {}",
                frames / span, span, over_40, over_100);
    const auto percentile = cadence.Percentiles();
    REXLOG_INFO("DS2_CADENCE kind=guest_HandleEvents_hook samples={} intervals={} overflow={} span_s={:.6f} p50_ms={:.6f} p95_ms={:.6f} p99_ms={:.6f} deadline_ms={:.6f} deadline_misses={} unknown_deadline={} percentile=nearest_rank",
                cadence.count, cadence.intervals, cadence.overflow, span, percentile[0],
                percentile[1], percentile[2], deadline_ms, cadence.misses, cadence.unknown_deadline);
    cadence = {};
    frames = over_40 = over_100 = 0;
    window = now;
  }
}

void BeforeTimeUpdate(ds2::mod::Call& c) {
  static const bool log_fps = DS2_SETTING_NUMBER("log_fps", 0) != 0;
  static double cadence_deadline_ms = 0;
  if (log_fps) LogFps(cadence_deadline_ms);
  static const int vblanks = int(DS2_SETTING_NUMBER("target_vblanks", 1));
  if (vblanks < 1 || vblanks > 4) return;
  static const TimeAddrs* build = nullptr;
  if (!build && !(build = FindBuild(c))) return;  // TV system not initialised yet
  const uint32_t kTargetFrameMs = build->target_frame_ms;
  const float want = c.load_f32(build->one_vblank_ms) * float(vblanks);
  cadence_deadline_ms = want;  // Actual guest vblank value and selected mod target.
  if (c.load_f32(kTargetFrameMs) == want) return;
  static bool logged = false;
  if (!logged) {
    logged = true;
    REXLOG_INFO("framerate-mod: target frame {:.3f} ms -> {:.3f} ms ({} vblank(s), {:.1f} FPS)",
                c.load_f32(kTargetFrameMs), want, vblanks, 1000.0 / double(want));
  }
  uint32_t u;
  std::memcpy(&u, &want, 4);
  u = std::byteswap(u);
  std::memcpy(c.base + kTargetFrameMs, &u, 4);
}

void BeforeFrameLock(ds2::mod::Call& c) {
  static const int vblanks = int(DS2_SETTING_NUMBER("target_vblanks", 1));
  if (vblanks < 1 || vblanks > 4) return;
  const int32_t game = int32_t(c.ctx.r3.u32);
  if (game > vblanks) c.ctx.r3.u64 = uint32_t(vblanks);
  static bool logged = false;
  if (!logged) {
    logged = true;
    REXLOG_INFO("framerate-mod: Displ_WaitForFrameLock({}) -> {}", game, c.ctx.r3.u32);
  }
}
}  // namespace

DS2_HOOK_BEFORE("EARS::Time::TimeManager::HandleEvents", BeforeTimeUpdate);
DS2_HOOK_BEFORE("Displ_WaitForFrameLock", BeforeFrameLock);
