// DS2 TU2 adapter. Protocol: src_tu2/XDK/.../multidisc.cpp:133 and
// src_tu2/DeadSpace/src/modules/multidisc/multidisc.cpp:685,1311.
// Persistent-device/transaction design reference: doc-haz/infinite-undiscovery-recomp
// 614a0a0ebd72103fce8aaa52c715d847b703a53e, src/disc_swap.h:138-181,237-310.
// This implementation preserves DS2's separate TU2 game: mount.
#include "ds2_rex_diagnostic_pch.h"
#include "ds2_disc_swap.h"
#include <rex/cvar.h>
#include <rex/filesystem/devices/disc_image_device.h>
#include <rex/filesystem/devices/host_path_device.h>
#include <rex/filesystem/vfs.h>
#include <rex/runtime.h>
#include <rex/system/kernel_state.h>
#include <rex/system/function_dispatcher.h>
#include <rex/system/util/xex2_info.h>
#include <rex/system/xevent.h>
#include <array>
#include <cstring>
#include <stdexcept>
using rex::X_STATUS; // SDK status macros use this unqualified typedef.

REXCVAR_DEFINE_STRING(ds2_disc1_iso, "", "DS2", "Verified Disc 1 ISO available for automatic swaps.");
REXCVAR_DEFINE_STRING(ds2_disc2_iso, "", "DS2", "Optional verified Disc 2 ISO available for automatic swaps.");

namespace ds2::disc_swap {
namespace {
using namespace rex::filesystem;
constexpr std::array<const char*, 2> kDevices{"\\Device\\Ds2Disc1", "\\Device\\Ds2Disc2"};
constexpr std::array<const char*, 2> kAliases{"d:", "\\Device\\Cdrom0"};
constexpr const char* kUpdate = "\\Device\\Ds2TitleUpdate";
// Disc identity measured in launcher/input-evidence.json and test-inputs/disc2/default.json.
constexpr uint32_t kTitleId = 0x454108DF;
VirtualFileSystem* fs = nullptr;
std::array<Entry*, 2> roots{};
Entry* update_root = nullptr;
unsigned current_disc = 0;

struct CloseFile { void operator()(File* f) const { if (f) f->Destroy(); } };
template <typename T> T Read(File& file, size_t offset) {
  T value{};
  const size_t size = file.entry()->size();
  if (offset > size || sizeof(T) > size - offset) throw std::runtime_error("Truncated disc XEX header");
  size_t count = 0;
  if (file.ReadSync({reinterpret_cast<uint8_t*>(&value), sizeof(T)}, offset, &count) != X_STATUS_SUCCESS ||
      count != sizeof(T)) throw std::runtime_error("Cannot read disc XEX header");
  return value;
}

unsigned Identify(Device& device) {
  auto* entry = device.ResolvePath("default.xex");
  File* raw = nullptr;
  if (!entry || entry->Open(FileAccess::kGenericRead, &raw) != X_STATUS_SUCCESS)
    throw std::runtime_error("Disc has no readable default.xex");
  std::unique_ptr<File, CloseFile> file(raw);
  // SDK system/util/xex2_info.h supplies the on-disc layout and endian conversion.
  auto header = Read<rex::xex2_header>(*file, 0);
  if (header.magic != 0x58455832 || header.header_count >
      (entry->size() - offsetof(rex::xex2_header, headers)) / sizeof(rex::xex2_opt_header))
    throw std::runtime_error("Invalid disc XEX header");
  for (uint32_t i = 0; i < header.header_count; ++i) {
    auto opt = Read<rex::xex2_opt_header>(*file, offsetof(rex::xex2_header, headers) + i * sizeof(rex::xex2_opt_header));
    if (opt.key != rex::XEX_HEADER_EXECUTION_INFO) continue;
    auto info = Read<rex::xex2_opt_execution_info>(*file, opt.offset);
    if (info.title_id != kTitleId || info.disc_count != 2 || info.disc_number < 1 || info.disc_number > 2)
      throw std::runtime_error("ISO is not a Dead Space 2 two-disc release");
    const auto marker = "disc" + std::to_string(info.disc_number) + "_info.txt";
    if (!device.ResolvePath(marker)) throw std::runtime_error("DS2 disc identity marker is missing");
    REXLOG_INFO("DS2_DISC_ID disc={} title={:08X} media={:08X}", info.disc_number,
                uint32_t(info.title_id), uint32_t(info.media_id));
    return info.disc_number;
  }
  throw std::runtime_error("Disc XEX execution info missing");
}

unsigned Mount(const std::filesystem::path& path, unsigned expected = 0) {
  // Identify before registration, so a rejected input cannot alter aliases.
  auto disc = std::make_unique<DiscImageDevice>("\\Device\\Ds2Inspect", path);
  if (!disc->Initialize()) throw std::runtime_error("Cannot open disc ISO: " + path.string());
  const auto number = Identify(*disc);
  if (expected && number != expected) throw std::runtime_error("Configured disc number does not match ISO");
  if (roots[number - 1]) return number;
  disc = std::make_unique<DiscImageDevice>(kDevices[number - 1], path);
  if (!disc->Initialize()) throw std::runtime_error("Cannot initialize disc mount");
  auto* root = disc->ResolvePath("");
  if (!root || !fs->RegisterDevice(std::move(disc))) throw std::runtime_error("Cannot register disc mount");
  roots[number - 1] = root;
  return number;
}

// Caller holds the SDK global critical region, also used by VFS path resolution.
// Devices are never removed: outstanding files keep their original Entry/Device alive.
bool Select(unsigned number) {
  if (number < 1 || number > roots.size() || !roots[number - 1]) return false;
  if (fs->ResolvePath("game:") != update_root) return false;
  struct Previous { bool present; std::string target; };
  std::array<Previous, kAliases.size()> previous;
  for (size_t i = 0; i < kAliases.size(); ++i)
    previous[i].present = fs->FindSymbolicLink(kAliases[i], previous[i].target);
  try {
    for (auto alias : kAliases) {
      fs->UnregisterSymbolicLink(alias);
      if (!fs->RegisterSymbolicLink(alias, kDevices[number - 1]) ||
          fs->ResolvePath(alias) != roots[number - 1]) throw std::runtime_error("Alias verification failed");
    }
    if (fs->ResolvePath("game:") != update_root) throw std::runtime_error("TU2 mapping changed");
  } catch (...) {
    for (size_t i = 0; i < kAliases.size(); ++i) {
      fs->UnregisterSymbolicLink(kAliases[i]);
      if (previous[i].present && !fs->RegisterSymbolicLink(kAliases[i], previous[i].target))
        throw std::runtime_error("Disc alias rollback failed");
    }
    return false;
  }
  current_disc = number;
  REXLOG_INFO("DS2_DISC_SELECT disc={} device={} tu2_preserved=true", number, kDevices[number - 1]);
  return true;
}
} // namespace

void Prepare(rex::Runtime& runtime, const std::filesystem::path& boot_iso,
             const std::filesystem::path& update_directory) {
  if (!std::filesystem::is_regular_file(update_directory / "default.xexp"))
    throw std::runtime_error("TU2 requires default.xexp next to default.xex");
  fs = runtime.file_system();
  const auto boot = Mount(boot_iso);
  const std::array<std::string, 2> paths{REXCVAR_GET(ds2_disc1_iso), REXCVAR_GET(ds2_disc2_iso)};
  for (unsigned i = 0; i < paths.size(); ++i) if (!paths[i].empty()) Mount(paths[i], i + 1);
  auto update = std::make_unique<HostPathDevice>(kUpdate, update_directory, true);
  if (!update->Initialize()) throw std::runtime_error("Cannot initialize TU2 mount");
  update_root = update->ResolvePath("");
  if (!update_root || !fs->RegisterDevice(std::move(update))) throw std::runtime_error("Cannot register TU2 mount");
  auto lock = rex::thread::global_critical_region::AcquireDirect();
  fs->UnregisterSymbolicLink("game:");
  if (!fs->RegisterSymbolicLink("game:", kUpdate) || !Select(boot))
    throw std::runtime_error("Cannot select boot disc and TU2 mapping");
}
} // namespace ds2::disc_swap

// Strong application symbol resolves generated direct calls and the import map.
// No IAT patching and no changes to the SDK or generated guest instructions.
REX_EXTERN(__imp__XamSwapDisc) {
  using namespace ds2::disc_swap;
  const uint32_t requested = ctx.r3.u32;
  const uint32_t event_address = ctx.r4.u32;
  ctx.r3.u64 = X_STATUS_UNSUCCESSFUL;
  REXLOG_INFO("DS2_DISC_REQUEST requested={} current={} event={:08X} lr={:08X}",
              requested, current_disc, event_address, uint32_t(ctx.lr));
  try {
    if (!fs || !event_address) throw std::runtime_error("Swap has no mount state or completion event");
    auto* kernel = rex::system::kernel_state();
    // GetNativeObject's optional type is a native dispatcher type, NOT XObject::Type.
    // Follow xeKeSetEvent's default decoding, then check before casting.
    auto event = rex::system::XObject::GetNativeObject(kernel, base + event_address);
    if (!event || event->type() != rex::system::XObject::Type::Event)
      throw std::runtime_error("Invalid swap completion event");
    auto lock = rex::thread::global_critical_region::AcquireDirect();
    if (!Select(requested)) throw std::runtime_error("Requested disc unavailable or alias transaction failed");
    // Same operation as SDK xeKeSetEvent, after obtaining/validating the native event.
    static_cast<rex::system::XEvent*>(event.get())->Set(1, false);
    ctx.r3.u64 = X_STATUS_SUCCESS;
    REXLOG_INFO("DS2_DISC_COMPLETE disc={} event={:08X}", requested, event_address);
  } catch (const std::exception& error) {
    REXLOG_ERROR("DS2_DISC_FAILED requested={} reason={}", requested, error.what());
    // DS2 waits on the event, not the return code: leave it pending on failure.
  }
}

void ds2::disc_swap::RegisterImport(rex::Runtime& runtime) {
  // TU2 generated registration: src_tu2/ds2_rex_diagnostic_register.cpp.
  if (!runtime.function_dispatcher()->SetFunction(0x82B0857C, __imp__XamSwapDisc))
    throw std::runtime_error("Cannot register DS2 TU2 swap import");
}

// Small observation hooks retain the original guest bodies and log their results.
REX_EXTERN(__imp__EARS__Modules__MultiDiscManager__DetermineDisc);
REX_EXTERN(EARS__Modules__MultiDiscManager__DetermineDisc) {
  __imp__EARS__Modules__MultiDiscManager__DetermineDisc(ctx, base);
  REXLOG_INFO("DS2_DISC_DETECTED disc={}", ctx.r3.u32);
}
REX_EXTERN(__imp__EARS__MainLoop__LevelManager__StartLevel);
REX_EXTERN(__imp__EARS__Modules__MultiDiscManager__OnExitStateWaitingForDisc);
REX_EXTERN(EARS__Modules__MultiDiscManager__OnExitStateWaitingForDisc) {
  const auto manager = ctx.r3.u32;
  __imp__EARS__Modules__MultiDiscManager__OnExitStateWaitingForDisc(ctx, base);
  REXLOG_INFO("DS2_GUEST_SWAP_COMPLETE disc={} event_handle={} popup={}",
              REX_LOAD_U32(manager + 20), REX_LOAD_U32(manager + 40), REX_LOAD_U32(manager + 132));
}
REX_EXTERN(EARS__MainLoop__LevelManager__StartLevel) {
  const auto manager = ctx.r3.u32;
  // Inline EARS String: pointer at +48, BE16 length at +52, as read by
  // MultiDiscManager::GetDiscNumberForChapter (src_tu2/.../multidisc.cpp).
  const auto chars = REX_LOAD_U32(manager + 48);
  const auto length = REX_LOAD_U16(manager + 52);
  const std::string chapter = chars ? std::string(reinterpret_cast<char*>(base + chars), length) : "";
  // LevelManager ctor's required map String is +40; chapter +48 is optional.
  const auto map_chars = REX_LOAD_U32(manager + 40);
  const auto map_length = REX_LOAD_U16(manager + 44);
  const std::string map = map_chars ? std::string(reinterpret_cast<char*>(base + map_chars), map_length) : "";
  __imp__EARS__MainLoop__LevelManager__StartLevel(ctx, base);
  // src_tu2/DeadSpace/src/main/levelmanager.cpp StartLevel stores state 16 at +24.
  REXLOG_INFO("DS2_LEVEL_STARTED state={} map=\"{}\" chapter=\"{}\"", REX_LOAD_U32(manager + 24), map, chapter);
}
