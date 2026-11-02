#pragma once
// 48kHz同率のclock補正用。係数とscratchはReader生成時（非RT）に確保する。
#include <array>
#include <algorithm>
#include <cmath>
#include <limits>
#include <numbers>

namespace auralyn {
struct Sinc48 {
    static constexpr size_t taps = 128, phases = 256, past = 63, future = 64, cache_frames = 2200;
    std::array<std::array<float, taps>, phases + 1> coefficients{};
    std::array<float, cache_frames> left{}, right{};
    Sinc48() {
        constexpr double cutoff = .93;
        for (size_t phase = 0; phase <= phases; phase++) {
            std::array<double, taps> weights{};
            double sum = 0;
            for (size_t k = 0; k < taps; k++) {
                const double x = double(k) - past - double(phase) / phases;
                const double angle = std::numbers::pi * x / 64;
                const double window = std::abs(x) >= 64 ? 0 : .35875 + .48829 * std::cos(angle)
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
}
