// Intro skip mod.
//
// The boot intro is one movie (movies/en/trio_frontend.vp6: EA logo, Visceral logo, the
// title logo forming). The presentation side plays a movie with
// EARS::RtMovie::RtMoviePlayer::Play(this = r3, const char* file = r4, ...): it copies the
// name to this+12 (128 bytes), creates a rw::movie::MoviePlayer2 and stores it at this+152.
// RtMoviePlayer::RenderFrame asks that player rw::movie::MoviePlayer2::IsFinished(player = r3)
// every frame; RtMoviePlayer::Stop (also called at the start of every Play) tears it down.
//
//   AFTER  RtMoviePlayer::Play      a movie whose name contains one of settings.match marks
//                                   ITS player (this+152) and the time;
//   AFTER  MoviePlayer2::IsFinished only for the marked player, and only once it has played
//                                   settings.end_after_ms, report "finished" (r3 = 1), so the
//                                   game ends that movie through its own end-of-movie path;
//   BEFORE RtMoviePlayer::Stop      stopping the marked movie clears the mark.
//
// No other movie player is touched. (v1.0 forced IsFinished on ANY player while a match was
// pending: docs/evidence/ui-regression-2026-10-07.) Every Play is logged with its file name.
#include "ds2_modapi.h"

#include <rex/logging.h>

#include <atomic>
#include <chrono>
#include <string>
#include <vector>

namespace {
using Clock = std::chrono::steady_clock;
constexpr uint32_t kFileName = 12, kPlayer = 152;
std::atomic<uint32_t> g_rt{0}, g_player{0};  // the marked RtMoviePlayer and its MoviePlayer2
std::atomic<int64_t> g_play_ns{0};
std::atomic<int> g_forced{0};

std::vector<std::string> Patterns() {
  std::vector<std::string> out;
  std::string s = DS2_SETTING_STRING("match", "");
  size_t p = 0;
  while (p < s.size()) {
    size_t q = s.find(',', p);
    if (q == std::string::npos) q = s.size();
    if (q > p) out.push_back(s.substr(p, q - p));
    p = q + 1;
  }
  return out;
}
int64_t NowNs() { return Clock::now().time_since_epoch().count(); }

void AfterPlay(ds2::mod::Call& c) {
  static const std::vector<std::string> pats = Patterns();
  uint32_t self = uint32_t(c.entry_r[3]);
  if (!self) return;
  std::string file(c.c_str(self + kFileName), 0, 127);
  file = file.c_str();
  uint32_t player = c.load<uint32_t>(self + kPlayer);
  for (const auto& p : pats) {
    if (file.find(p) == std::string::npos || !player) continue;
    g_play_ns.store(NowNs());
    g_player.store(player);
    g_rt.store(self);
    REXLOG_INFO("intro-skip: RtMoviePlayer::Play this={:08X} '{}' player={:08X} matches '{}': skipping",
                self, file, player, p);
    return;
  }
  REXLOG_INFO("intro-skip: RtMoviePlayer::Play this={:08X} '{}' player={:08X} (not skipped)", self, file,
              player);
}

void AfterIsFinished(ds2::mod::Call& c) {
  static const int64_t after_ns = int64_t(DS2_SETTING_NUMBER("end_after_ms", 0) * 1e6);
  uint32_t player = uint32_t(c.entry_r[3]);
  if (!player || player != g_player.load() || c.ctx.r3.u32 != 0) return;
  int64_t played = NowNs() - g_play_ns.load();
  if (played < after_ns) return;
  c.ctx.r3.u64 = 1;
  if (g_forced.fetch_add(1) == 0)
    REXLOG_INFO("intro-skip: MoviePlayer2::IsFinished({:08X}) forced to true after {} ms (marked player)", player,
                played / 1000000);
}

void BeforeStop(ds2::mod::Call& c) {
  uint32_t self = c.ctx.r3.u32;
  uint32_t want = g_rt.load();
  if (!self || self != want || c.load<uint32_t>(self + kPlayer) != g_player.load()) return;
  if (g_rt.compare_exchange_strong(want, 0)) {
    g_player.store(0);
    REXLOG_INFO("intro-skip: RtMoviePlayer::Stop this={:08X}: marked movie ended after {} ms ({} forced IsFinished)",
                self, (NowNs() - g_play_ns.load()) / 1000000, g_forced.exchange(0));
  }
}
}  // namespace

DS2_HOOK_AFTER("EARS::RtMovie::RtMoviePlayer::Play", AfterPlay);
DS2_HOOK_AFTER("rw::movie::MoviePlayer2::IsFinished", AfterIsFinished);
DS2_HOOK_BEFORE("EARS::RtMovie::RtMoviePlayer::Stop", BeforeStop);
