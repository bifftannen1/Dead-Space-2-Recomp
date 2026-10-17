#pragma once
#include <filesystem>
namespace rex { class Runtime; }
namespace ds2::disc_swap {
// Called before guest execution. Throws if a configured disc is not a DS2 pair.
void Prepare(rex::Runtime& runtime, const std::filesystem::path& boot_iso,
             const std::filesystem::path& update_directory);
void RegisterImport(rex::Runtime& runtime);
}
