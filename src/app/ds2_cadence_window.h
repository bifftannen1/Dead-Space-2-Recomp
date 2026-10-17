#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>

namespace ds2::provenance {
// Bounded samples from the guest HandleEvents hook, not displayed frames.
template <size_t Capacity = 4096>
struct CadenceWindow {
  std::array<double, Capacity> samples{};
  size_t count = 0, intervals = 0, overflow = 0, misses = 0, unknown_deadline = 0;
  void Add(double ms, double deadline_ms) {
    ++intervals;
    if (count < Capacity) samples[count++] = ms;
    else ++overflow;
    if (deadline_ms > 0) misses += ms > deadline_ms;
    else ++unknown_deadline;
  }
  std::array<double, 3> Percentiles() const {
    if (!count) return {};
    auto sorted = samples;
    std::sort(sorted.begin(), sorted.begin() + count);
    // Nearest rank, one-based ceil(p * n), clamped for a one-sample window.
    const auto rank = [&](double p) { return sorted[size_t(std::ceil(p * count)) - 1]; };
    return {rank(.50), rank(.95), rank(.99)};
  }
};
}  // namespace ds2::provenance
