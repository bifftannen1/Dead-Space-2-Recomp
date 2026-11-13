// stat("") and lstat("") as on Linux, linked in by
// ps5_package.sh with --wrap=stat --wrap=lstat.
//
// The PS5 answers an empty path with EINVAL where Linux says ENOENT, and
// std::filesystem::exists("") throws on anything but "not found": an empty
// optional path (no config file) ended the app at startup.
#include <errno.h>
#include <sys/stat.h>

int __real_stat(const char* path, struct stat* st);
int __real_lstat(const char* path, struct stat* st);

__attribute__((visibility("hidden"))) int __wrap_stat(const char* path, struct stat* st) {
  if (path && path[0] == 0) {
    errno = ENOENT;
    return -1;
  }
  return __real_stat(path, st);
}

__attribute__((visibility("hidden"))) int __wrap_lstat(const char* path, struct stat* st) {
  if (path && path[0] == 0) {
    errno = ENOENT;
    return -1;
  }
  return __real_lstat(path, st);
}
