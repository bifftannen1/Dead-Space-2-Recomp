// FOV mod: scales the vertical field of view of the game's 3D render contexts.
//
// RenderContext::RC_RebuildAndApplyProjectionMatrix(RenderContext* this = r3) builds the
// projection from this->perspective.fovRadians (+428) when this->projectionType (+416) is
// perspective (1), via TS_ProjectionSetPerspectiveCustomNearFar. A BEFORE hook writes the
// scaled FOV into the context, an AFTER hook puts the game's value back, so the scale never
// compounds and nothing else that reads the field sees a changed value.
// Contexts with aspect ratio 1 (square shadow-map views) are left alone.
//
// Field offsets come from the PDB struct header (tools/modapi/gen_types.py, DS2_TYPES_PDB);
// when it is not generated the same offsets are used as constants, checked against it when
// it is.
#include "ds2_modapi.h"

#include <rex/logging.h>

#include <atomic>

#if __has_include("ds2_types.h")
#include "ds2_types.h"
static_assert(offsetof(ds2::pdb::RenderContext, projectionType) == 416);
static_assert(offsetof(ds2::pdb::RenderContext, perspective) == 428);
#define DS2_FOV_TYPED 1
#define DS2_FOV_TYPED_STR " [ds2::pdb::RenderContext]"
#else
#define DS2_FOV_TYPED 0
#define DS2_FOV_TYPED_STR " [raw offsets]"
#endif

#include <bit>
#include <cstring>

namespace {
constexpr uint32_t kProjectionType = 416, kFov = 428, kAspect = 432;
thread_local uint32_t t_ctx = 0;
thread_local float t_saved = 0;
std::atomic<int> g_calls{0};

void BeforeProjection(ds2::mod::Call& c) {
  static const float scale = float(DS2_SETTING_NUMBER("fov_scale", 1.0));
  static const int log_n = int(DS2_SETTING_NUMBER("log_calls", 5));
  uint32_t rc = c.ctx.r3.u32;
  t_ctx = 0;
  if (!rc || c.load<uint32_t>(rc + kProjectionType) != 1) return;
  float fov = c.load_f32(rc + kFov), aspect = c.load_f32(rc + kAspect);
  if (aspect == 1.0f) return;
  float scaled = fov * scale;
#if DS2_FOV_TYPED
  auto* ctx = ds2::guest<ds2::pdb::RenderContext>(c.base, rc);
  ctx->perspective.fovRadians = scaled;
#else
  uint32_t u;
  std::memcpy(&u, &scaled, 4);
  u = std::byteswap(u);
  std::memcpy(c.base + rc + kFov, &u, 4);
#endif
  t_ctx = rc;
  t_saved = fov;
  int n = g_calls.fetch_add(1) + 1;
  if (n <= log_n || (n & (n - 1)) == 0)
    REXLOG_INFO("fov-mod: projection {} ctx={:08X} aspect={:.4f} fov {:.5f} -> {:.5f} rad (x{:.3f}){}", n,
                rc, aspect, fov, scaled, scale, DS2_FOV_TYPED_STR);
}

void AfterProjection(ds2::mod::Call& c) {
  if (!t_ctx) return;
  uint32_t u;
  std::memcpy(&u, &t_saved, 4);
  u = std::byteswap(u);
  std::memcpy(c.base + t_ctx + kFov, &u, 4);
  t_ctx = 0;
}
}  // namespace

DS2_HOOK_BEFORE("RenderContext::RC_RebuildAndApplyProjectionMatrix", BeforeProjection);
DS2_HOOK_AFTER("RenderContext::RC_RebuildAndApplyProjectionMatrix", AfterProjection);
