// Dead Space 2 for PS5 - see ds2_ps5_install.h.
#include "ds2_ps5_install.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <memory>
#include <system_error>

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <fmt/format.h>

#include <rex/filesystem/devices/disc_image_device.h>
#include <rex/filesystem/devices/stfs_container_device.h>
#include <rex/filesystem/entry.h>
#include <rex/filesystem/file.h>
#include <rex/logging.h>
#include <rex/system/util/xex2_info.h>
#include <rex/system/xcontent.h>

#include "sha256.h"

namespace ds2ps5 {

namespace fs = std::filesystem;
using rex::X_STATUS;  // (the SDK's status macros name it unqualified)

namespace {

constexpr uint32_t kTitleId = 0x454108DF;

// The exact files src_tu2/ was recompiled from (tools/build.py, launcher/).
struct DiscPins {
  const char* xex_sha256;
  const char* xexp_sha256;
  const char* patch_path;  // inside Title Update #2
};
constexpr DiscPins kPins[2] = {
    {"928597ddb80240b8aefeef0a70a9476ee85d6de733035b326dd155c2094a3408",
     "8d386d37e8f71937b9d9c4b34bdbf85240101479c8aaece1002d94e7cdc01ec2", "disc001\\default.xexp"},
    {"78d834922b39a68de45b0da319b9d90d28d62d7db1d12e0c8446dbf302c18a6e",
     "dd5aaf4edba09cc332c75bb2027bfe308a077c52c411df2070fdf3193372fb5e", "disc002\\default.xexp"},
};

std::string Sha256(const std::vector<uint8_t>& data) {
  sha256::SHA256 hash;
  hash.add(data.data(), data.size());
  return hash.getHash();
}

std::string Lower(std::string s) {
  std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return char(std::tolower(c)); });
  return s;
}

bool ReadWholeFile(const fs::path& path, std::vector<uint8_t>& out, size_t limit) {
  std::error_code ec;
  const auto size = fs::file_size(path, ec);
  if (ec || size > limit) return false;
  out.resize(size_t(size));
  std::FILE* f = std::fopen(path.c_str(), "rb");
  if (!f) return false;
  const size_t got = out.empty() ? 0 : std::fread(out.data(), 1, out.size(), f);
  std::fclose(f);
  return got == out.size();
}

// Written under a temporary name and renamed, so a file is either whole or absent.
bool WriteWholeFile(const fs::path& path, const uint8_t* data, size_t size) {
  std::error_code ec;
  fs::create_directories(path.parent_path(), ec);
  const fs::path temporary = path.string() + ".part";
  std::FILE* f = std::fopen(temporary.c_str(), "wb");
  if (!f) return false;
  const size_t put = size ? std::fwrite(data, 1, size, f) : 0;
  const bool ok = std::fclose(f) == 0 && put == size;
  if (!ok) {
    fs::remove(temporary, ec);
    return false;
  }
  fs::rename(temporary, path, ec);
  return !ec;
}

struct CloseFile {
  void operator()(rex::filesystem::File* f) const {
    if (f) f->Destroy();
  }
};

// A whole file out of a mounted image or package.
bool ReadEntry(rex::filesystem::Entry* entry, std::vector<uint8_t>& out, size_t limit) {
  if (!entry || entry->size() > limit) return false;
  rex::filesystem::File* raw = nullptr;
  if (entry->Open(rex::filesystem::FileAccess::kGenericRead, &raw) != X_STATUS_SUCCESS || !raw) return false;
  std::unique_ptr<rex::filesystem::File, CloseFile> file(raw);
  out.resize(entry->size());
  size_t done = 0;
  while (done < out.size()) {
    size_t got = 0;
    const size_t want = std::min<size_t>(out.size() - done, size_t(4) << 20);
    if (file->ReadSync({out.data() + done, want}, done, &got) != X_STATUS_SUCCESS || !got) return false;
    done += got;
  }
  return true;
}

// 1 or 2 for a Dead Space 2 disc image's default.xex, 0 for anything else.
int DiscNumberOf(const std::vector<uint8_t>& xex) {
  if (xex.size() < sizeof(rex::xex2_header)) return 0;
  rex::xex2_header header;
  std::memcpy(&header, xex.data(), sizeof(header));
  if (header.magic != 0x58455832) return 0;  // 'XEX2'
  const size_t first = offsetof(rex::xex2_header, headers);
  const uint32_t count = header.header_count;
  if (count > (xex.size() - first) / sizeof(rex::xex2_opt_header)) return 0;
  for (uint32_t i = 0; i < count; ++i) {
    rex::xex2_opt_header opt;
    std::memcpy(&opt, xex.data() + first + i * sizeof(opt), sizeof(opt));
    if (opt.key != rex::XEX_HEADER_EXECUTION_INFO) continue;
    const uint32_t offset = opt.offset;
    if (offset > xex.size() || sizeof(rex::xex2_opt_execution_info) > xex.size() - offset) return 0;
    rex::xex2_opt_execution_info info;
    std::memcpy(&info, xex.data() + offset, sizeof(info));
    if (info.title_id != kTitleId || info.disc_count != 2) return 0;
    return info.disc_number == 1 || info.disc_number == 2 ? info.disc_number : 0;
  }
  return 0;
}

bool HasMagic(const fs::path& path, const char* a, const char* b = nullptr, const char* c = nullptr) {
  char magic[4] = {};
  std::FILE* f = std::fopen(path.c_str(), "rb");
  if (!f) return false;
  const bool read = std::fread(magic, 1, 4, f) == 4;
  std::fclose(f);
  if (!read) return false;
  for (const char* m : {a, b, c}) {
    if (m && std::memcmp(magic, m, 4) == 0) return true;
  }
  return false;
}

// Every file in a mounted package, depth first, with its path inside it.
void WalkFiles(rex::filesystem::Entry* entry, const fs::path& relative,
               std::vector<std::pair<rex::filesystem::Entry*, fs::path>>& out) {
  for (const auto& child : entry->children()) {
    const fs::path path = relative / child->name();
    if (child->attributes() & rex::filesystem::kFileAttributeDirectory) {
      WalkFiles(child.get(), path, out);
    } else {
      out.emplace_back(child.get(), path);
    }
  }
}

// A DLC package unpacked where the runtime mounts DLC from:
// <content root>/0000000000000000/<title id>/<content type>/<package file name>/
// (the layout tools/extract_dlc.py writes on the PC).
bool InstallDlc(const fs::path& package, uint32_t content_type, const fs::path& content_root,
                std::vector<std::string>& notes) {
  const fs::path destination = content_root / "0000000000000000" / fmt::format("{:08X}", kTitleId) /
                               fmt::format("{:08X}", content_type) / package.filename();
  const fs::path marker = destination / ".ds2ps5_installed";
  std::error_code ec;
  if (fs::exists(marker, ec)) return true;
  rex::filesystem::StfsContainerDevice device("\\Device\\Ds2Ps5Dlc", package);
  if (!device.Initialize()) {
    notes.push_back("DLC " + package.filename().string() + ": the package can't be read");
    return false;
  }
  rex::filesystem::Entry* root = device.ResolvePath("");
  if (!root) return false;
  std::vector<std::pair<rex::filesystem::Entry*, fs::path>> files;
  WalkFiles(root, {}, files);
  uint64_t bytes = 0;
  std::vector<uint8_t> chunk(size_t(4) << 20);
  for (const auto& [entry, relative] : files) {
    const fs::path out_path = destination / relative;
    fs::create_directories(out_path.parent_path(), ec);
    rex::filesystem::File* raw = nullptr;
    if (entry->Open(rex::filesystem::FileAccess::kGenericRead, &raw) != X_STATUS_SUCCESS || !raw) {
      notes.push_back("DLC " + package.filename().string() + ": can't open " + relative.string());
      return false;
    }
    std::unique_ptr<rex::filesystem::File, CloseFile> file(raw);
    std::FILE* out = std::fopen(out_path.c_str(), "wb");
    if (!out) {
      notes.push_back("DLC: can't write " + out_path.string());
      return false;
    }
    size_t done = 0;
    bool ok = true;
    while (ok && done < entry->size()) {
      size_t got = 0;
      const size_t want = std::min(chunk.size(), entry->size() - done);
      ok = file->ReadSync({chunk.data(), want}, done, &got) == X_STATUS_SUCCESS && got &&
           std::fwrite(chunk.data(), 1, got, out) == got;
      done += got;
    }
    ok = (std::fclose(out) == 0) && ok && done == entry->size();
    if (!ok) {
      notes.push_back("DLC " + package.filename().string() + ": " + relative.string() + " didn't copy whole");
      return false;
    }
    bytes += done;
  }
  if (std::FILE* f = std::fopen(marker.c_str(), "w")) {
    std::fprintf(f, "%zu files, %llu bytes\n", files.size(), static_cast<unsigned long long>(bytes));
    std::fclose(f);
  }
  notes.push_back(fmt::format("DLC {} installed ({} files, {} MB)", package.filename().string(), files.size(),
                              bytes >> 20));
  return true;
}

}  // namespace

GameFiles PrepareGameFiles(const fs::path& app_root, const fs::path& content_root) {
  GameFiles result;
  std::error_code ec;
  const fs::path install = app_root / "install";
  fs::create_directories(install, ec);

  // What the player gave us: disc images, packages (title update, DLC) and
  // loose patch files, in install/ (subfolders included) and next to the app.
  std::vector<fs::path> candidates;
  for (fs::recursive_directory_iterator it(install, fs::directory_options::skip_permission_denied, ec), end;
       !ec && it != end; it.increment(ec)) {
    if (it.depth() > 3) {
      it.disable_recursion_pending();
      continue;
    }
    if (it->is_regular_file(ec)) candidates.push_back(it->path());
  }
  for (fs::directory_iterator it(app_root, ec), end; !ec && it != end; it.increment(ec)) {
    if (it->is_regular_file(ec) && Lower(it->path().extension().string()) == ".iso") {
      candidates.push_back(it->path());
    }
  }
  std::sort(candidates.begin(), candidates.end());

  std::vector<uint8_t> xex[2], patch[2];
  std::vector<fs::path> dlc_packages;
  std::vector<uint32_t> dlc_types;
  for (const fs::path& path : candidates) {
    const std::string extension = Lower(path.extension().string());
    const std::string name = Lower(path.filename().string());
    if (extension == ".iso") {
      rex::filesystem::DiscImageDevice disc("\\Device\\Ds2Ps5Inspect", path);
      if (!disc.Initialize()) {
        result.notes.push_back(path.filename().string() + ": not an Xbox 360 disc image this can read");
        continue;
      }
      std::vector<uint8_t> image_xex;
      if (!ReadEntry(disc.ResolvePath("default.xex"), image_xex, size_t(64) << 20)) {
        result.notes.push_back(path.filename().string() + ": no default.xex in it");
        continue;
      }
      const int number = DiscNumberOf(image_xex);
      if (!number) {
        result.notes.push_back(path.filename().string() + ": not a Dead Space 2 disc");
        continue;
      }
      if (Sha256(image_xex) != kPins[number - 1].xex_sha256) {
        result.notes.push_back(fmt::format(
            "{}: Disc {} of a different release (this build is made from the World (En,Fr,Es) discs)",
            path.filename().string(), number));
        continue;
      }
      if (result.iso[number - 1].empty()) {
        result.iso[number - 1] = path;
        xex[number - 1] = std::move(image_xex);
        result.notes.push_back(fmt::format("Disc {}: {}", number, path.filename().string()));
      }
    } else if (name == "default.xexp") {
      // A patch already taken out of the title update: told apart by its hash.
      std::vector<uint8_t> data;
      if (!ReadWholeFile(path, data, size_t(16) << 20)) continue;
      const std::string hash = Sha256(data);
      for (int d = 0; d < 2; ++d) {
        if (hash == kPins[d].xexp_sha256 && patch[d].empty()) patch[d] = data;
      }
    } else if (HasMagic(path, "LIVE", "PIRS", "CON ")) {
      auto header = rex::filesystem::StfsContainerDevice::ReadPackageHeader(path);
      if (!header || header->metadata.execution_info.title_id != kTitleId) continue;
      const uint32_t type = uint32_t(rex::XContentType(header->metadata.content_type));
      if (type == uint32_t(rex::XContentType::kInstaller)) {
        rex::filesystem::StfsContainerDevice update("\\Device\\Ds2Ps5Update", path);
        if (!update.Initialize()) {
          result.notes.push_back(path.filename().string() + ": the title update can't be read");
          continue;
        }
        for (int d = 0; d < 2; ++d) {
          std::vector<uint8_t> data;
          if (patch[d].empty() && ReadEntry(update.ResolvePath(kPins[d].patch_path), data, size_t(16) << 20) &&
              Sha256(data) == kPins[d].xexp_sha256) {
            patch[d] = std::move(data);
          }
        }
        result.notes.push_back(fmt::format("Title update package: {}{}", path.filename().string(),
                                           patch[0].empty() && patch[1].empty()
                                               ? " (not Title Update #2 for these discs)"
                                               : ""));
      } else if (type == uint32_t(rex::XContentType::kMarketplaceContent)) {
        dlc_packages.push_back(path);
        dlc_types.push_back(type);
      }
    }
  }

  // Stage default.xex + default.xexp per disc. What is staged already (an
  // earlier start) is used as it is when it still has the right hashes, so
  // the title update package needn't stay in install/.
  for (int d = 0; d < 2; ++d) {
    const fs::path directory = app_root / "game" / fmt::format("disc{}", d + 1);
    const fs::path xex_path = directory / "default.xex", patch_path = directory / "default.xexp";
    std::vector<uint8_t> staged;
    const bool xex_ok = ReadWholeFile(xex_path, staged, size_t(64) << 20) && Sha256(staged) == kPins[d].xex_sha256;
    const bool patch_ok =
        ReadWholeFile(patch_path, staged, size_t(16) << 20) && Sha256(staged) == kPins[d].xexp_sha256;
    bool ready = true;
    if (!xex_ok) {
      ready = !xex[d].empty() && WriteWholeFile(xex_path, xex[d].data(), xex[d].size());
      if (ready) result.notes.push_back(fmt::format("Disc {}: default.xex staged", d + 1));
    }
    if (ready && !patch_ok) {
      ready = !patch[d].empty() && WriteWholeFile(patch_path, patch[d].data(), patch[d].size());
      if (ready) result.notes.push_back(fmt::format("Disc {}: Title Update #2 patch staged", d + 1));
    }
    // (the disc image itself is the game's data: without it there is nothing to run)
    if (ready && !result.iso[d].empty()) result.game_dir[d] = directory;
  }

  for (size_t i = 0; i < dlc_packages.size(); ++i) {
    InstallDlc(dlc_packages[i], dlc_types[i], content_root, result.notes);
  }

  if (!result.boot_disc()) {
    const bool any_iso = !result.iso[0].empty() || !result.iso[1].empty();
    const bool any_patch = !patch[0].empty() || !patch[1].empty();
    if (!any_iso) {
      result.problem =
          "No Dead Space 2 disc image was found.\n\n"
          "Copy your own Dead Space 2 (World) (En,Fr,Es) Xbox 360 Disc 1 image (.iso) into this app's "
          "install folder, and Disc 2 as well if you have it:\n\n"
          "    " + install.string() + "\n\n"
          "Then start the app again.";
    } else if (!any_patch) {
      result.problem =
          "The disc image was found, but Title Update #2 is missing.\n\n"
          "Copy the Dead Space 2 Title Update #2 package (TU_12K226V_000000C000000.00000000000G3, as it is, "
          "no extension) into this app's install folder:\n\n"
          "    " + install.string() + "\n\n"
          "Then start the app again. Title Update #1 or an update for another release won't do.";
    } else {
      result.problem =
          "The game's files were found but couldn't be prepared (is the drive full or read-only?).\n\n"
          "The log in this app's logs folder has the details.";
    }
  }
  return result;
}

}  // namespace ds2ps5
