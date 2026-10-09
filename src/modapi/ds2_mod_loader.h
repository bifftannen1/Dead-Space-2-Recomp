// Dead Space 2 PC recomp - mod loader (manifests, hook activation, loose-file overlay).
// Header-only; included once, by app/src/main_mods.cpp (DS2_MODS=ON builds only).
//
//   <mods dir>/<name>.toml   manifest: [mod] name, version, enabled, load_order, hooks,
//                            sources (build time), files (loose-file root), [settings]
//   <mods dir>/<files root>  host files laid over the disc: <root>/a/b.txt answers game:\a\b.txt
//
// Load order: ascending load_order, then name. BEFORE handlers run in load order, AFTER
// handlers in load order. Two enabled mods that REPLACE the same function, or that provide
// the same file, are a conflict: it is reported (log + summary) and the later mod in load
// order wins. Nothing is silently merged.
#pragma once

#include "ds2_modapi.h"

#include <rex/cvar.h>
#include <rex/filesystem.h>
#include <rex/filesystem/devices/host_path_device.h>
#include <rex/filesystem/entry.h>
#include <rex/filesystem/vfs.h>
#include <rex/logging.h>

#include <toml++/toml.hpp>

#include <cstdio>
#include <filesystem>
#include <memory>
#include <set>
#include <string>
#include <vector>

REXCVAR_DEFINE_BOOL(ds2_mods, true, "DS2", "Load mods from ds2_mods_dir (mods/*.toml)");
REXCVAR_DEFINE_STRING(ds2_mods_dir, "", "DS2", "Mods folder. Empty: <exe folder>/mods");
REXCVAR_DEFINE_BOOL(ds2_mods_trace_opens, false, "DS2",
                    "Log every disc file the game opens (mounts the overlay even with no files)");

namespace ds2::mod {

struct Manifest {
  std::string name, version, description, files;
  bool enabled = true;
  int load_order = 0;
  std::vector<std::string> hooks;
  std::filesystem::path path;
  toml::table settings;
};

namespace detail {
inline std::vector<std::string>& conflicts() {
  static std::vector<std::string> c;
  return c;
}
inline void StoreSettings(const std::string& mod, const toml::table& t) {
  auto& dst = settings_store()[mod];
  dst.clear();
  for (auto& [k, v] : t) {
    Setting st;
    if (auto d = v.value<double>()) { st.has_num = true; st.num = *d; }
    if (auto b = v.value<bool>()) { st.has_bool = true; st.b = *b; }
    if (auto x = v.value<std::string>()) { st.has_str = true; st.str = *x; }
    dst[std::string(k.str())] = st;
  }
}
}  // namespace detail

inline std::filesystem::path ModsDir() {
  std::string d = REXCVAR_GET(ds2_mods_dir);
  if (!d.empty()) return d;
  return rex::filesystem::GetExecutableFolder() / "mods";
}

inline std::vector<Manifest> LoadManifests(const std::filesystem::path& dir) {
  std::vector<Manifest> out;
  std::error_code ec;
  if (!std::filesystem::is_directory(dir, ec)) return out;
  std::vector<std::filesystem::path> files;
  for (const auto& e : std::filesystem::directory_iterator(dir, ec))
    if (e.path().extension() == ".toml" && e.is_regular_file(ec)) files.push_back(e.path());
  std::sort(files.begin(), files.end());
  std::set<std::string> names;
  for (const auto& p : files) {
    toml::table t;
    try {
      t = toml::parse_file(p.string());
    } catch (const toml::parse_error& err) {
      REXLOG_ERROR("ds2-mods: {}: parse error: {}", p.string(), err.description());
      detail::conflicts().push_back("manifest parse error: " + p.string());
      continue;
    }
    Manifest m;
    m.path = p;
    auto mod = t["mod"];
    m.name = mod["name"].value_or(std::string{});
    m.version = mod["version"].value_or(std::string{"0"});
    m.description = mod["description"].value_or(std::string{});
    m.enabled = mod["enabled"].value_or(true);
    m.load_order = int(mod["load_order"].value_or(int64_t{0}));
    m.files = mod["files"].value_or(std::string{});
    if (auto* hooks = mod["hooks"].as_array())
      for (auto& h : *hooks)
        if (auto s = h.value<std::string>()) m.hooks.push_back(*s);
    if (auto* st = t["settings"].as_table()) m.settings = *st;
    if (m.name.empty()) {
      REXLOG_ERROR("ds2-mods: {}: [mod] name missing; skipped", p.string());
      detail::conflicts().push_back("manifest without name: " + p.string());
      continue;
    }
    if (!names.insert(m.name).second) {
      REXLOG_ERROR("ds2-mods: {}: duplicate mod name '{}'; skipped", p.string(), m.name);
      detail::conflicts().push_back("duplicate mod name " + m.name + ": " + p.string());
      continue;
    }
    out.push_back(std::move(m));
  }
  std::stable_sort(out.begin(), out.end(), [](const Manifest& a, const Manifest& b) {
    return a.load_order != b.load_order ? a.load_order < b.load_order : a.name < b.name;
  });
  return out;
}

// Turns the statically registered handlers of enabled mods on. Called once, before the guest
// runs. Handlers of mods with no manifest or enabled=false stay off.
inline void Finalize(const std::vector<Manifest>& mods) {
  std::map<std::string, int> order;
  for (size_t i = 0; i < mods.size(); ++i)
    if (mods[i].enabled) order[mods[i].name] = int(i);
  auto& r = detail::registry();
  std::lock_guard lk(r.mu);
  size_t active_slots = 0, active_handlers = 0;
  for (auto& [addr, s] : r.slots) {
    s.before.clear();
    s.after.clear();
    s.replace = {};
    for (auto h : s.registered) {
      auto it = order.find(h.mod);
      if (it == order.end()) continue;
      h.order = it->second;
      if (h.kind == Kind::kBefore) s.before.push_back(h);
      if (h.kind == Kind::kAfter) s.after.push_back(h);
      if (h.kind == Kind::kReplace) {
        if (s.replace.fn && s.replace.order <= h.order) {
          auto msg = std::string("REPLACE conflict on ") + s.name + ": " + s.replace.mod + " and " +
                     h.mod + "; " + h.mod + " (later in load order) wins";
          REXLOG_ERROR("ds2-mods: {}", msg);
          detail::conflicts().push_back(msg);
          s.replace = h;
        } else if (s.replace.fn) {
          auto msg = std::string("REPLACE conflict on ") + s.name + ": " + h.mod + " and " +
                     s.replace.mod + "; " + s.replace.mod + " (later in load order) wins";
          REXLOG_ERROR("ds2-mods: {}", msg);
          detail::conflicts().push_back(msg);
        } else {
          s.replace = h;
        }
      }
    }
    auto by = [](const detail::Handler& a, const detail::Handler& b) { return a.order < b.order; };
    std::stable_sort(s.before.begin(), s.before.end(), by);
    std::stable_sort(s.after.begin(), s.after.end(), by);
    size_t n = s.before.size() + s.after.size() + (s.replace.fn ? 1 : 0);
    if (n && !s.original) {
      // A handler whose address has no dispatcher would never run: refuse loudly.
      auto msg = std::string("hook on ") + s.name + " has no dispatcher (not in a manifest's hooks)";
      REXLOG_ERROR("ds2-mods: {}", msg);
      detail::conflicts().push_back(msg);
      continue;
    }
    s.active.store(n != 0, std::memory_order_release);
    if (n) {
      ++active_slots;
      active_handlers += n;
      REXLOG_INFO("ds2-mods: hook {:08X} {} : {} before, {} after, replace={}", addr, s.name,
                  s.before.size(), s.after.size(), s.replace.fn ? s.replace.mod : "-");
    }
  }
  r.finalized = true;
  REXLOG_INFO("ds2-mods: {} hooked functions, {} active handlers", active_slots, active_handlers);
}

//=============================================================================
// Loose-file overlay: a merged entry tree over the disc. Each entry forwards Open/OpenMapped
// to the disc entry, or to a mod's host file when a mod provides that path.
//=============================================================================

inline uint64_t Fnv1a64File(const std::filesystem::path& p, uint64_t* size_out) {
  uint64_t h = 1469598103934665603ull, n = 0;
  if (FILE* f = _wfopen(p.c_str(), L"rb")) {
    unsigned char buf[1 << 16];
    size_t got;
    while ((got = fread(buf, 1, sizeof buf, f)) > 0) {
      for (size_t i = 0; i < got; ++i) h = (h ^ buf[i]) * 1099511628211ull;
      n += got;
    }
    fclose(f);
  }
  if (size_out) *size_out = n;
  return h;
}

class OverlayDevice;

class OverlayEntry : public rex::filesystem::Entry {
 public:
  OverlayEntry(rex::filesystem::Device* dev, Entry* parent, std::string_view path, Entry* target,
               std::string layer)
      : Entry(dev, parent, path), target_(target), layer_(std::move(layer)) {
    Retarget(target, layer_);
  }
  void Retarget(Entry* t, std::string layer) {
    target_ = t;
    layer_ = std::move(layer);
    attributes_ = t->attributes();
    size_ = t->size();
    allocation_size_ = t->allocation_size();
    create_timestamp_ = t->create_timestamp();
    access_timestamp_ = t->access_timestamp();
    write_timestamp_ = t->write_timestamp();
  }
  void AddChild(std::unique_ptr<Entry> e) { children_.push_back(std::move(e)); }
  Entry* target() const { return target_; }
  const std::string& layer() const { return layer_; }
  std::filesystem::path host_path;  // set for mod-provided files

  rex::X_STATUS Open(uint32_t desired_access, rex::filesystem::File** out_file) override;
  bool can_map() const override { return target_->can_map(); }
  std::unique_ptr<rex::memory::MappedMemory> OpenMapped(rex::memory::MappedMemory::Mode mode,
                                                        size_t offset, size_t length) override {
    Trace("map");
    return target_->OpenMapped(mode, offset, length);
  }

 private:
  void Trace(const char* how);
  Entry* target_;
  std::string layer_;  // "" = disc
  bool logged_ = false;
};

class OverlayDevice : public rex::filesystem::Device {
 public:
  // `base_root` is the root entry of the device game: pointed at (the disc image, or the
  // extracted-disc folder). That device stays registered and owned by the VFS; the overlay
  // only references its entries.
  OverlayDevice(std::string_view mount, rex::filesystem::Entry* base_root, bool trace)
      : Device(mount), base_root_(base_root), disc_(base_root->device()), trace_(trace) {}

  // A mod's files root, in load order (later layers win).
  void AddLayer(const std::string& mod, const std::filesystem::path& root) {
    auto dev = std::make_unique<rex::filesystem::HostPathDevice>(
        "\\Device\\ds2mod\\" + mod, root, /*read_only=*/true);
    layers_.push_back({mod, root, std::move(dev)});
  }

  bool Initialize() override {
    for (auto& l : layers_)
      if (!l.dev->Initialize()) {
        REXLOG_ERROR("ds2-mods: cannot read files root {} of {}", l.root.string(), l.mod);
        return false;
      }
    root_ = std::make_unique<OverlayEntry>(this, nullptr, "", base_root_, "");
    Mirror(root_.get(), base_root_);
    for (auto& l : layers_) Lay(root_.get(), l.dev->ResolvePath(""), l);
    return true;
  }
  void Dump(rex::string::StringBuffer* sb) override { root_->Dump(sb, 0); }
  rex::filesystem::Entry* ResolvePath(const std::string_view path) override {
    return root_->ResolvePath(path);
  }
  const std::string& name() const override { return disc_->name(); }
  uint32_t attributes() const override { return disc_->attributes(); }
  uint32_t component_name_max_length() const override { return disc_->component_name_max_length(); }
  uint32_t total_allocation_units() const override { return disc_->total_allocation_units(); }
  uint32_t available_allocation_units() const override { return disc_->available_allocation_units(); }
  uint32_t sectors_per_allocation_unit() const override { return disc_->sectors_per_allocation_unit(); }
  uint32_t bytes_per_sector() const override { return disc_->bytes_per_sector(); }

  bool trace() const { return trace_; }
  size_t replaced = 0, added = 0;

 private:
  struct Layer {
    std::string mod;
    std::filesystem::path root;
    std::unique_ptr<rex::filesystem::HostPathDevice> dev;
  };

  static std::string Join(const std::string& a, const std::string& b) {
    return a.empty() ? b : a + "\\" + b;
  }

  void Mirror(OverlayEntry* dst, rex::filesystem::Entry* src) {
    for (auto& c : src->children()) {
      auto e = std::make_unique<OverlayEntry>(this, dst, Join(dst->path(), c->name()), c.get(), "");
      Mirror(e.get(), c.get());
      dst->AddChild(std::move(e));
    }
  }

  void Lay(OverlayEntry* dst, rex::filesystem::Entry* src, Layer& l) {
    if (!src) return;
    for (auto& c : src->children()) {
      auto* have = static_cast<OverlayEntry*>(dst->GetChild(c->name()));
      bool is_dir = c->attributes() & rex::filesystem::kFileAttributeDirectory;
      std::string guest = Join(dst->path(), c->name());
      if (have && is_dir) {
        Lay(have, c.get(), l);
        continue;
      }
      std::filesystem::path host = l.root / std::filesystem::path(guest);
      if (have) {
        if (have->target()->attributes() & rex::filesystem::kFileAttributeDirectory) {
          auto msg = "file " + guest + " of " + l.mod + " shadows a disc directory; ignored";
          REXLOG_ERROR("ds2-mods: {}", msg);
          detail::conflicts().push_back(msg);
          continue;
        }
        if (!have->layer().empty()) {
          auto msg = "FILE conflict on " + guest + ": " + have->layer() + " and " + l.mod + "; " +
                     l.mod + " (later in load order) wins";
          REXLOG_ERROR("ds2-mods: {}", msg);
          detail::conflicts().push_back(msg);
        } else {
          ++replaced;
        }
        have->Retarget(c.get(), l.mod);
        have->host_path = host;
        REXLOG_INFO("ds2-mods: {} replaces game:\\{} ({} bytes, disc had another copy)", l.mod,
                    guest, c->size());
        continue;
      }
      auto e = std::make_unique<OverlayEntry>(this, dst, guest, c.get(), l.mod);
      e->host_path = host;
      ++added;
      REXLOG_INFO("ds2-mods: {} adds game:\\{}{}", l.mod, guest, is_dir ? "\\" : "");
      if (is_dir) Lay(e.get(), c.get(), l);
      dst->AddChild(std::move(e));
    }
  }

  rex::filesystem::Entry* base_root_;
  rex::filesystem::Device* disc_;
  std::vector<Layer> layers_;
  std::unique_ptr<OverlayEntry> root_;
  bool trace_;
};

inline void OverlayEntry::Trace(const char* how) {
  auto* dev = static_cast<OverlayDevice*>(device_);
  if (!layer_.empty() && !logged_) {
    logged_ = true;
    uint64_t n = 0;
    uint64_t h = Fnv1a64File(host_path, &n);
    REXLOG_INFO("ds2-mods: game opened game:\\{} ({}) -> {} [{}] {} bytes fnv1a64={:016x}", path(),
                how, host_path.string(), layer_, n, h);
  } else if (dev->trace()) {
    REXLOG_INFO("ds2-mods: open({}) game:\\{} {} bytes{}", how, path(), size(),
                layer_.empty() ? "" : " [mod " + layer_ + "]");
  }
}

inline rex::X_STATUS OverlayEntry::Open(uint32_t desired_access, rex::filesystem::File** out_file) {
  Trace("open");
  return target_->Open(desired_access, out_file);
}

struct LoadResult {
  std::vector<Manifest> mods;
  size_t enabled = 0;
};

// Reads the manifests and turns hooks on. The overlay is mounted later (MountOverlay), once
// the VFS exists.
inline LoadResult LoadMods() {
  LoadResult r;
  if (!REXCVAR_GET(ds2_mods)) {
    REXLOG_INFO("ds2-mods: disabled (--ds2_mods=false)");
    Finalize({});
    return r;
  }
  auto dir = ModsDir();
  r.mods = LoadManifests(dir);
  for (auto& m : r.mods) {
    detail::StoreSettings(m.name, m.settings);
    if (m.enabled) ++r.enabled;
    REXLOG_INFO("ds2-mods: manifest {} {} v{} load_order={} {} hooks={} files={}", m.path.filename().string(),
                m.name, m.version, m.load_order, m.enabled ? "enabled" : "disabled", m.hooks.size(),
                m.files.empty() ? "-" : m.files);
  }
  // Each manifest hook name must be one the binary carries a dispatcher for.
  {
    std::set<std::string> carried;
    for (auto& e : gen::kHookTable) carried.insert(e.name);
    for (auto& m : r.mods)
      for (auto& h : m.hooks)
        if (!carried.count(h)) {
          auto msg = "mod " + m.name + " lists hook '" + h + "' that this build has no dispatcher for (rebuild with the manifest in mods/)";
          REXLOG_ERROR("ds2-mods: {}", msg);
          detail::conflicts().push_back(msg);
        }
  }
  Finalize(r.mods);
  REXLOG_INFO("ds2-mods: {} manifests in {}, {} enabled", r.mods.size(), dir.string(), r.enabled);
  return r;
}

// Lays the enabled mods' files over game: and d:. The device game: points at (the disc image
// or the extracted-disc folder) stays registered as it is; an overlay device is registered at
// \Device\Ds2Overlay0 and game: (and d:, when it points at the same device) are re-linked to it.
// With no file layers (and no tracing) nothing is mounted: no-mods behaviour is the plain build.
inline bool MountOverlay(rex::filesystem::VirtualFileSystem* vfs, const LoadResult& lr) {
  bool trace = REXCVAR_GET(ds2_mods_trace_opens);
  std::vector<const Manifest*> layered;
  for (auto& m : lr.mods)
    if (m.enabled && !m.files.empty()) layered.push_back(&m);
  if (layered.empty() && !trace) {
    REXLOG_INFO("ds2-mods: no file layers; game: left as mounted");
    return true;
  }
  std::string target, d_target;
  if (!vfs->FindSymbolicLink("game:", target)) {
    REXLOG_ERROR("ds2-mods: game: is not a symbolic link; cannot overlay");
    return false;
  }
  auto* base_root = vfs->ResolvePath(target);
  if (!base_root) {
    REXLOG_ERROR("ds2-mods: game: -> {} does not resolve", target);
    return false;
  }
  constexpr const char* kMount = "\\Device\\Ds2Overlay0";
  auto ov = std::make_unique<OverlayDevice>(kMount, base_root, trace);
  auto dir = ModsDir();
  for (auto* m : layered) {
    auto root = dir / m->files;
    std::error_code ec;
    if (!std::filesystem::is_directory(root, ec)) {
      auto msg = "mod " + m->name + ": files root " + root.string() + " is not a folder";
      REXLOG_ERROR("ds2-mods: {}", msg);
      detail::conflicts().push_back(msg);
      continue;
    }
    ov->AddLayer(m->name, root);
  }
  if (!ov->Initialize()) return false;
  auto* dev = ov.get();
  if (!vfs->RegisterDevice(std::move(ov))) return false;
  bool d_same = vfs->FindSymbolicLink("d:", d_target) && d_target == target;
  vfs->UnregisterSymbolicLink("game:");
  vfs->RegisterSymbolicLink("game:", kMount);
  if (d_same) {
    vfs->UnregisterSymbolicLink("d:");
    vfs->RegisterSymbolicLink("d:", kMount);
  }
  REXLOG_INFO("ds2-mods: overlay {} over {} (game:{}): {} layers, {} files replaced, {} added{}", kMount,
              target, d_same ? ", d:" : "", layered.size(), dev->replaced, dev->added,
              trace ? ", tracing opens" : "");
  return true;
}

inline void ReportConflicts() {
  auto& c = detail::conflicts();
  REXLOG_INFO("ds2-mods: {} conflicts/errors", c.size());
  for (auto& s : c) REXLOG_INFO("ds2-mods:   {}", s);
}

}  // namespace ds2::mod
