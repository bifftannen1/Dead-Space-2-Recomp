// Dead Space 2 PC recomp - mod API (hooks). Header-only. docs/MODDING.md.
//
// A hook names a guest function by its PDB name. tools/modapi/gen_hooks.py resolves every
// name the mods use to exactly one guest address at configure time (config/
// symbols_lane5_admitted.tsv, cross-checked against the codegen function table) and writes
// two files into the build tree:
//   ds2_hook_table.h      the name -> address table that ds2::mod::hook_addr() reads at
//                         compile time (an unknown or ambiguous name is a compile error);
//   ds2_hook_dispatch.cpp one strong `sub_XXXXXXXX` per hooked address. The generated code
//                         defines every guest function as a weak alias of `__imp__sub_X`
//                         (generated/<project>_pch.h, DEFINE_REX_FUNC); a strong definition
//                         in another object wins at link time. That is the SDK's own
//                         override mechanism (rex/hook.h REX_HOOK); no generated file changes.
//
//   DS2_HOOK_BEFORE("EARS::Framework::CameraManager::GetActiveCamFov", fn);   // fn(Call&)
//   DS2_HOOK_AFTER (...);    // runs after the original; the return value is in c.ctx
//   DS2_HOOK_REPLACE(...);   // runs instead of the original; c.call_original() is available
//
// Handlers belong to the mod named by DS2_MOD_ID (the build passes -DDS2_MOD_ID="<name>" for
// each mod's sources) and only run when that mod is enabled in its manifest (mods/*.toml).
// With DS2_MODS=OFF none of this is compiled: the app is the plain build.
#pragma once

#include <rex/ppc/context.h>

#include <algorithm>
#include <atomic>
#include <bit>
#include <cstdint>
#include <cstring>
#include <initializer_list>
#include <map>
#include <mutex>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

#include "ds2_hook_table.h"  // generated (build tree): ds2::mod::gen::kHookTable

#ifndef DS2_MOD_ID
#define DS2_MOD_ID "app"
#endif

namespace ds2::mod {

using GuestFn = void (*)(PPCContext&, uint8_t*);

enum class Kind : uint8_t { kBefore, kAfter, kReplace };

inline const char* KindName(Kind k) {
  return k == Kind::kBefore ? "before" : k == Kind::kAfter ? "after" : "replace";
}

// One call of a hooked guest function.
struct Call {
  PPCContext& ctx;  // live guest registers: args on entry (r3..r10, f1..f13), result after
  uint8_t* base;    // guest memory base
  GuestFn original_;
  uint32_t address;
  const char* name;
  // Argument registers as they were on entry (after every BEFORE handler ran).
  uint64_t entry_r[11]{};  // entry_r[3]..entry_r[10]
  double entry_f[14]{};    // entry_f[1]..entry_f[13]
  bool original_called = false;

  void call_original() {
    original_called = true;
    original_(ctx, base);
  }
  // Guest memory helpers (big-endian guest, little-endian host).
  template <typename T>
  T load(uint32_t ea) const {
    T v;
    std::memcpy(&v, base + ea, sizeof(T));
    if constexpr (sizeof(T) > 1) v = std::byteswap(v);
    return v;
  }
  float load_f32(uint32_t ea) const {
    uint32_t u = load<uint32_t>(ea);
    float f;
    std::memcpy(&f, &u, 4);
    return f;
  }
  const char* c_str(uint32_t ea) const { return ea ? reinterpret_cast<const char*>(base + ea) : ""; }
};

using HandlerFn = void (*)(Call&);

namespace detail {

struct Handler {
  HandlerFn fn;
  const char* mod;
  Kind kind;
  int order = 0;  // mod load order, set at Finalize
};

struct Slot {
  uint32_t address = 0;
  const char* name = "";
  GuestFn original = nullptr;
  std::vector<Handler> registered;  // everything the binary carries
  // Active set, built once by Finalize() before the guest starts.
  std::vector<Handler> before, after;
  Handler replace{};
  std::atomic<bool> active{false};
  std::atomic<uint64_t> calls{0};
};

struct Registry {
  std::mutex mu;
  std::map<uint32_t, Slot> slots;
  bool finalized = false;
};

inline Registry& registry() {
  static Registry r;
  return r;
}

inline Slot& SlotFor(uint32_t address) {
  auto& r = registry();
  std::lock_guard lk(r.mu);
  return r.slots[address];  // std::map: references stay valid
}

// Called by the generated dispatcher (once per hooked address, function-local static).
inline Slot* BindSlot(uint32_t address, const char* name, GuestFn original) {
  Slot& s = SlotFor(address);
  s.address = address;
  s.name = name;
  s.original = original;
  return &s;
}

struct Registrar {
  Registrar(Kind kind, const char* mod, uint32_t address, const char* name, HandlerFn fn) {
    Slot& s = SlotFor(address);
    s.address = address;
    s.name = name;
    s.registered.push_back(Handler{fn, mod, kind});
  }
};

inline void Dispatch(Slot& s, PPCContext& ctx, uint8_t* base) {
  if (!s.active.load(std::memory_order_acquire)) {
    s.original(ctx, base);
    return;
  }
  s.calls.fetch_add(1, std::memory_order_relaxed);
  Call c{ctx, base, s.original, s.address, s.name};
  for (auto& h : s.before) h.fn(c);
  c.entry_r[3] = ctx.r3.u64;
  c.entry_r[4] = ctx.r4.u64;
  c.entry_r[5] = ctx.r5.u64;
  c.entry_r[6] = ctx.r6.u64;
  c.entry_r[7] = ctx.r7.u64;
  c.entry_r[8] = ctx.r8.u64;
  c.entry_r[9] = ctx.r9.u64;
  c.entry_r[10] = ctx.r10.u64;
  c.entry_f[1] = ctx.f1.f64;
  c.entry_f[2] = ctx.f2.f64;
  c.entry_f[3] = ctx.f3.f64;
  c.entry_f[4] = ctx.f4.f64;
  if (s.replace.fn)
    s.replace.fn(c);
  else
    c.call_original();
  for (auto& h : s.after) h.fn(c);
}

}  // namespace detail

// Compile-time name -> guest function, for the names a manifest lists under `calls`.
consteval GuestFn call_fn(std::string_view name) {
  for (const auto& e : gen::kCallTable)
    if (e.fn && name == e.name) return e.fn;
  throw "ds2 mod API: call name not in the generated table (list it under the manifest's calls)";
}

// Calls a guest function with up to 8 integer/pointer arguments (r3..r10) on a copy of the
// hook's registers (same stack pointer, so the callee's frame goes below the current one).
// Returns r3. The call goes through sub_<VA>, so other mods' hooks on that function run too.
inline uint64_t CallGuest(Call& c, GuestFn fn, std::initializer_list<uint64_t> args) {
  PPCContext sub = c.ctx;
  PPCRegister* regs[8] = {&sub.r3, &sub.r4, &sub.r5, &sub.r6, &sub.r7, &sub.r8, &sub.r9, &sub.r10};
  size_t i = 0;
  for (uint64_t a : args) regs[i++]->u64 = a;
  fn(sub, c.base);
  c.ctx.fpscr = sub.fpscr;
  return sub.r3.u64;
}

// Manifest [settings], filled by the loader before the guest runs.
struct Setting {
  bool has_num = false, has_bool = false, has_str = false;
  double num = 0;
  bool b = false;
  std::string str;
};
namespace detail {
inline std::map<std::string, std::map<std::string, Setting>>& settings_store() {
  static std::map<std::string, std::map<std::string, Setting>> s;
  return s;
}
inline const Setting* FindSetting(const char* mod, const char* key) {
  auto& s = settings_store();
  auto it = s.find(mod);
  if (it == s.end()) return nullptr;
  auto jt = it->second.find(key);
  return jt == it->second.end() ? nullptr : &jt->second;
}
}  // namespace detail
inline double SettingNumber(const char* mod, const char* key, double def) {
  auto* s = detail::FindSetting(mod, key);
  return s && s->has_num ? s->num : def;
}
inline bool SettingBool(const char* mod, const char* key, bool def) {
  auto* s = detail::FindSetting(mod, key);
  return s && s->has_bool ? s->b : def;
}
inline std::string SettingString(const char* mod, const char* key, const char* def) {
  auto* s = detail::FindSetting(mod, key);
  return s && s->has_str ? s->str : std::string(def);
}

// Compile-time name -> address. Fails to compile for a name the generator did not resolve
// (unknown, ambiguous, or not a codegen function): the generator lists every hook name the
// mod sources use, and only names that resolved to exactly one address are in the table.
consteval uint32_t hook_addr(std::string_view name) {
  for (const auto& e : gen::kHookTable)
    if (name == e.name) return e.address;
  throw "ds2 mod API: hook name not in the generated table (see build/<app>/modapi_gen/gen_hooks.log)";
}

}  // namespace ds2::mod

#define DS2_MODAPI_CAT2(a, b) a##b
#define DS2_MODAPI_CAT(a, b) DS2_MODAPI_CAT2(a, b)
#define DS2_MODAPI_HOOK(kind, name, fn)                                                   \
  static ::ds2::mod::detail::Registrar DS2_MODAPI_CAT(ds2_hook_reg_, __COUNTER__)(       \
      kind, DS2_MOD_ID, ::ds2::mod::hook_addr(name), name, fn)

#define DS2_HOOK_BEFORE(name, fn) DS2_MODAPI_HOOK(::ds2::mod::Kind::kBefore, name, fn)
#define DS2_HOOK_AFTER(name, fn) DS2_MODAPI_HOOK(::ds2::mod::Kind::kAfter, name, fn)
#define DS2_HOOK_REPLACE(name, fn) DS2_MODAPI_HOOK(::ds2::mod::Kind::kReplace, name, fn)

// DS2_CALL(call, "Pdb::Name", args...): call a guest function named in the manifest's calls.
#define DS2_CALL(c, name, ...) \
  ::ds2::mod::CallGuest(c, std::integral_constant<::ds2::mod::GuestFn, ::ds2::mod::call_fn(name)>::value, {__VA_ARGS__})

// This mod's manifest [settings] (DS2_MOD_ID). Read them inside a handler, not at static init.
#define DS2_SETTING_NUMBER(key, def) ::ds2::mod::SettingNumber(DS2_MOD_ID, key, def)
#define DS2_SETTING_BOOL(key, def) ::ds2::mod::SettingBool(DS2_MOD_ID, key, def)
#define DS2_SETTING_STRING(key, def) ::ds2::mod::SettingString(DS2_MOD_ID, key, def)
