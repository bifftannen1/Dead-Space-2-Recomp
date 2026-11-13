// Dead Space 2 for PS5 - the precompiled header of the recompiled game code.
//
// src_tu2/ds2_rex_diagnostic_pch.h as it is checked in (it is part of the
// verified generated tree and stays byte for byte what the PC build compiles),
// with the one thing that differs on the PS5 set after it.
#pragma once

#include "ds2_rex_diagnostic_pch.h"

// The 0xE0000000 range (the Xbox 360's physical memory with 4 KB pages) starts
// 4 KB into the physical memory it mirrors. Where host memory is mapped in
// units bigger than 4 KB the view can't start there, so the runtime maps it
// from the start and adds the 4 KB to every address instead
// (rex::memory::detail::PhysicalHostOffset). The generated header knows that
// for Windows (64 KB units) and Apple Silicon (16 KB pages); the PS5 has 16 KB
// pages and 64 KB direct memory units and needs it too. The two must agree, or
// the game and the kernel calls it makes read and write 4 KB apart.
#undef REX_PHYS_HOST_OFFSET
#define REX_PHYS_HOST_OFFSET(addr) (((u32)(addr) >= 0xE0000000u) ? 0x1000u : 0u)
