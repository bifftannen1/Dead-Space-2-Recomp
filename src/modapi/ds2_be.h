// Dead Space 2 PC recomp - big-endian guest memory accessors. Header-only, no SDK dependency.
//
// The guest (Xbox 360, PowerPC) is big-endian; the host is little-endian. The PDB struct
// headers (tools/modapi/gen_types.py) type every multi-byte field as ds2::be<T>, so reading a
// field through a host pointer into guest memory gives the guest's value:
//
//   auto* cam = ds2::guest<ds2::pdb::EARS__Framework__CameraManager>(base, ctx.r3.u32);
//   float fov = cam->m_fov;           // be<float> -> float
//   cam->m_fov = fov * 1.25f;         // float -> big-endian store
//
// ds2::gptr<T> is a 32-bit guest pointer (the guest is ILP32): .get(base) gives a host T*.
// Every type here has alignment 1, so it can sit at any offset in a packed PDB layout.
#pragma once

#include <bit>
#include <cstdint>
#include <cstring>
#include <type_traits>

namespace ds2 {

namespace detail {
template <typename T>
struct be_rep {
  using type = std::conditional_t<sizeof(T) == 1, uint8_t,
               std::conditional_t<sizeof(T) == 2, uint16_t,
               std::conditional_t<sizeof(T) == 4, uint32_t, uint64_t>>>;
};
}  // namespace detail

template <typename T>
struct be {
  static_assert(std::is_trivially_copyable_v<T> && (sizeof(T) == 1 || sizeof(T) == 2 ||
                                                    sizeof(T) == 4 || sizeof(T) == 8));
  using rep = typename detail::be_rep<T>::type;
  unsigned char raw[sizeof(T)];

  T get() const {
    rep r;
    std::memcpy(&r, raw, sizeof r);
    if constexpr (sizeof(T) > 1) r = std::byteswap(r);
    T v;
    std::memcpy(&v, &r, sizeof v);
    return v;
  }
  void set(T v) {
    rep r;
    std::memcpy(&r, &v, sizeof r);
    if constexpr (sizeof(T) > 1) r = std::byteswap(r);
    std::memcpy(raw, &r, sizeof r);
  }
  operator T() const { return get(); }
  be& operator=(T v) {
    set(v);
    return *this;
  }
};

// 32-bit guest pointer to T (T may be void or an incomplete type).
template <typename T>
struct gptr {
  be<uint32_t> addr;
  uint32_t value() const { return addr.get(); }
  explicit operator bool() const { return addr.get() != 0; }
  T* get(uint8_t* base) const { return value() ? reinterpret_cast<T*>(base + value()) : nullptr; }
  void set(uint32_t guest_address) { addr.set(guest_address); }
};

// A host pointer to a guest object at guest address `ea`.
template <typename T>
inline T* guest(uint8_t* base, uint32_t ea) {
  return ea ? reinterpret_cast<T*>(base + ea) : nullptr;
}

static_assert(sizeof(be<uint32_t>) == 4 && alignof(be<uint32_t>) == 1);
static_assert(sizeof(be<double>) == 8 && alignof(be<double>) == 1);
static_assert(sizeof(gptr<void>) == 4 && alignof(gptr<void>) == 1);

}  // namespace ds2
