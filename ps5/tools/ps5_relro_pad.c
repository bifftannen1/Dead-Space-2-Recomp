// 2 KB of read-only-after-relocation data (a const array of
// pointers lands in .data.rel.ro in a PIE), linked in by ps5_package.sh only
// when the image's RELRO content ends too close to its page boundary for
// ps5-native-tool to put the PS5 process parameters after it. Shifting the
// end by half a page leaves it room; nothing reads this.
__attribute__((used, retain, visibility("hidden"))) void* const ps5_relro_pad[256] = {
    (void*)&ps5_relro_pad};
