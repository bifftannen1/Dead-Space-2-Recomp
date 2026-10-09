# Vulkan shared memory: kComputeWrite usage has no SHADER_WRITE access (resolve writes never made available)

Affects: ReXGlue SDK `src/graphics/vulkan/shared_memory.cpp` `VulkanSharedMemory::GetUsageMasks`,
and upstream Xenia `src/xenia/gpu/vulkan/vulkan_shared_memory.cc:454-457` (checked in
`xenia_tag360_oracle`). The same text is in both:

    case Usage::kComputeWrite:
      stage_mask = VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT;
      access_mask = VK_ACCESS_SHADER_READ_BIT;

`Use()` builds every barrier from `GetUsageMasks(last_usage_)` as the source. After a resolve
(`render_target_cache.cpp` `Use(kComputeWrite, extent)` -> copy-shader dispatch), the next
`Use(kRead)` (texture load) emits srcAccess = SHADER_READ. A barrier with no write bits in its source
access performs no availability operation, so the resolve's storage-buffer writes are not guaranteed
visible to the texture-load compute shader. The EDRAM buffer's own `EdramBufferUsage::kComputeWrite`
correctly uses SHADER_WRITE.

Observed: Dead Space 2 (Intel Arc B390, Intel driver 101.8724). A post effect does 4 partial resolves
(rect 264-640 x 0-288, dest_off_x 8/16) ping-ponging between 0A798000 and 0A888000, with a texture
load of each destination between them, all in one submission. Grid-aligned blocks appear with
readback_resolve=none. With readback_resolve=full (a queue wait after each resolve) the effect is
correct. The copy chain is identical in both modes, and 0 CPU invalidations hit the effect ranges
in the blocked frame.

Fix: `access_mask = VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT;` (patch:
`shared_memory_compute_write_fix.patch`). The scaled-resolve buffer path
(`VulkanTextureCache::UseScaledResolveBufferForWrite`) was not reviewed by this note.
