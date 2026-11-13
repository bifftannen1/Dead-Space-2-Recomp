// Dead Space 2 for PS5 - getting the game ready from the player's own files.
//
// The PC build is prepared by tools/build.py and the launcher (default.xex out
// of the Disc 1 image, default.xexp out of Title Update #2, the DLC package
// unpacked). A console has neither, so the app does the same at start-up, from
// whatever the player put in its install folder. Nothing is downloaded and no
// game data is part of the app.
#pragma once

#include <filesystem>
#include <string>
#include <vector>

namespace ds2ps5 {

struct GameFiles {
  // By disc number - 1. An image that is there and is that disc of Dead Space 2.
  std::filesystem::path iso[2];
  // game/disc<N>: default.xex and default.xexp, both checked against the hashes
  // the recompiled code was made from. Empty when that disc can't be started.
  std::filesystem::path game_dir[2];
  // What was found and done, for the log.
  std::vector<std::string> notes;
  // Why the game can't start (empty when it can), in words for the screen.
  std::string problem;

  // The disc to start from (1 or 2), 0 when neither is ready.
  int boot_disc() const { return !game_dir[0].empty() ? 1 : !game_dir[1].empty() ? 2 : 0; }
};

// Looks through <app>/install (and <app> itself) for disc images, Title Update
// #2 and DLC packages, stages what is missing under <app>/game, and unpacks DLC
// under <content_root> where the runtime's content manager looks for it.
GameFiles PrepareGameFiles(const std::filesystem::path& app_root,
                           const std::filesystem::path& content_root);

}  // namespace ds2ps5
