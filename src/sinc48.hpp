#pragma once
// 係数とscratchはReader生成時（非RT）に確保する。
#include <array>
#include <algorithm>
#include <cmath>
#include <limits>
#include <numbers>

namespace auralyn {
template <size_t Taps, size_t CacheFrames, int CutoffMilli, int CutoffDenominator = 1000>
struct SincBank {
    static constexpr size_t taps = Taps, phases = 256, past = taps / 2 - 1, future = taps / 2, cache_frames = CacheFrames;
    std::array<std::array<float, taps>, phases + 1> coefficients{};
    std::array<float, cache_frames> left{}, right{};
    SincBank() {
        constexpr double cutoff = double(CutoffMilli) / CutoffDenominator;
        for (size_t phase = 0; phase <= phases; phase++) {
            std::array<double, taps> weights{};
            double sum = 0;
            for (size_t k = 0; k < taps; k++) {
                const double x = double(k) - past - double(phase) / phases;
                const double angle = std::numbers::pi * x / future;
                const double window = std::abs(x) >= future ? 0 : .35875 + .48829 * std::cos(angle)
                    + .14128 * std::cos(2 * angle) + .01168 * std::cos(3 * angle);
                const double sinc = std::abs(x) < 1e-12 ? cutoff : std::sin(std::numbers::pi * cutoff * x) / (std::numbers::pi * x);
                weights[k] = window * sinc; sum += weights[k];
            }
            for (size_t k = 0; k < taps; k++) coefficients[phase][k] = float(weights[k] / sum);
        }
    }
    void sample(double position, float &l, float &r) const noexcept {
        const auto integral = size_t(position);
        const double scaled = (position - double(integral)) * phases;
        const auto phase = std::min(size_t(scaled), phases - 1);
        const float fraction = float(scaled - double(phase));
        double a = 0, b = 0;
        for (size_t k = 0; k < taps; k++) {
            const float weight = coefficients[phase][k] + fraction * (coefficients[phase + 1][k] - coefficients[phase][k]);
            a += double(left[integral + k]) * weight;
            b += double(right[integral + k]) * weight;
        }
        // 有限な極値の補間でもfloatへの変換で無限大を作らない。
        constexpr double limit = std::numeric_limits<float>::max();
        l = float(std::clamp(a, -limit, limit)); r = float(std::clamp(b, -limit, limit));
    }
};
using Sinc48 = SincBank<128, 2200, 930>;
using Sinc96 = SincBank<256, 4608, 465>;
using Sinc88 = SincBank<256, 4608, 44640, 88200>;
using Sinc176 = SincBank<512, 9216, 44640, 176400>;
using Sinc192 = SincBank<512, 9216, 44640, 192000>;
}
