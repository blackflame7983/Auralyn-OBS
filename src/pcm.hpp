#pragma once
// Windows x64のatomic相互運用に限定。正本と版はprotocol.lock.json。
#include <array>
#include <atomic>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#ifdef AURALYN_SINC48_POC
#include <memory>
#include "sinc48.hpp"
#endif

namespace auralyn {
constexpr uint32_t magic = 0x41555043;
constexpr uint32_t capacity = 16384, header_words = 16, slot_words = 3;
constexpr uint32_t words = header_words + capacity * slot_words, bytes = words * 4;
static_assert(std::atomic_ref<uint32_t>::is_always_lock_free);
static_assert(sizeof(float) == 4 && std::atomic_ref<uint32_t>::required_alignment <= 4);
inline float mono(float left, float right) noexcept { return left * .5f + right * .5f; }
inline uint32_t load(const uint32_t *p, size_t i) noexcept {
    // mappingは読み取り専用。loadは書込命令を使わず、全wordをatomicで読む。
    return std::atomic_ref<uint32_t>(const_cast<uint32_t &>(p[i])).load(std::memory_order_seq_cst);
}
inline bool valid(const uint32_t *p) noexcept {
    if (!p) return false;
    const auto rate = load(p, 6);
    return load(p, 0) == magic && load(p, 1) == 1 && load(p, 2) == 0 &&
        load(p, 3) == bytes && load(p, 4) == capacity && load(p, 5) == 2 &&
        (rate == 44100 || rate == 48000 || rate == 96000) && load(p, 7) != 0;
}
inline const char *interpolation_name(uint32_t value) noexcept {
    return value == 1 ? "sinc48-poc" : value == 2 ? "sinc96-to48-poc" : "linear-poc";
}
struct Stats {
    uint64_t blocks = 0, underruns = 0, overruns = 0, invalid = 0, stale = 0, resets = 0;
    uint32_t queued = 0, min_queued = UINT32_MAX, max_queued = 0;
    double correction_ppm = 0;
    uint32_t interpolation = 0;
};
class Reader {
#ifdef AURALYN_SINC48_POC
    // control側で生成・破棄。renderでは係数の再構築や割当を行わない。
    std::unique_ptr<Sinc48> sinc_ = std::make_unique<Sinc48>();
    std::unique_ptr<Sinc96> sinc96_ = std::make_unique<Sinc96>();
#endif
    uint32_t cursor_ = 0, epoch_ = UINT32_MAX, last_write_ = 0, source_rate_ = 0, output_rate_ = 0;
    uint64_t progress_ns_ = 0;
    double phase_ = 0;
    bool primed_ = false, stalled_ = false;
    bool frame(const uint32_t *p, uint32_t n, std::array<float, 2> &out) noexcept {
        const size_t index = header_words + (n & (capacity - 1)) * slot_words;
        const auto expected = n << 1;
        if (load(p, index) != expected) return false;
        const auto left = load(p, index + 1), right = load(p, index + 2);
        if (load(p, index) != expected) return false;
        std::memcpy(&out[0], &left, 4); std::memcpy(&out[1], &right, 4);
        for (auto &value : out) if (!std::isfinite(value)) value = 0;
        return true;
    }
    void reprime(uint32_t write) noexcept { cursor_ = write; phase_ = 0; primed_ = false; }
public:
    Stats stats;
    void disconnect() noexcept { epoch_ = UINT32_MAX; source_rate_ = 0; output_rate_ = 0; primed_ = false; phase_ = 0; stats.correction_ppm = 0; }
    bool render(const uint32_t *p, float *left, float *right, size_t frames, uint32_t obs_rate, uint64_t now) noexcept {
        std::fill_n(left, frames, 0.0f); std::fill_n(right, frames, 0.0f);
        if (!p) { disconnect(); return false; }
        if (!valid(p) || !obs_rate || !frames || frames > 2048) { stats.invalid++; disconnect(); return false; }
        const auto write = load(p, 10), epoch = load(p, 8), state = load(p, 9), rate = load(p, 6);
        if (rate != 44100 && rate != 48000 && rate != 96000) { stats.invalid++; disconnect(); return false; }
        if (epoch != epoch_ || rate != source_rate_ || obs_rate != output_rate_) {
            epoch_ = epoch; source_rate_ = rate; output_rate_ = obs_rate; reprime(write); last_write_ = write; progress_ns_ = now;
            stalled_ = false; stats.resets++; stats.correction_ppm = 0;
        }
        if (state != 1) { reprime(write); return false; }
        if (write != last_write_) { last_write_ = write; progress_ns_ = now; stalled_ = false; }
        if (now - progress_ns_ > 100000000) {
            if (!stalled_) stats.stale++;
            stalled_ = true; reprime(write); return false;
        }
        const auto target = rate / 20;
        uint32_t sinc_kind = 0;
        uint32_t history = 0, lookahead = 0;
#ifdef AURALYN_SINC48_POC
        if (obs_rate == 48000 && rate == 48000) { sinc_kind = 1; history = uint32_t(Sinc48::past); lookahead = uint32_t(Sinc48::future); }
        if (obs_rate == 48000 && rate == 96000) { sinc_kind = 2; history = uint32_t(Sinc96::past); lookahead = uint32_t(Sinc96::future); }
#endif
        stats.interpolation = sinc_kind;
        uint32_t available = write - cursor_;
        if (available >= capacity - 2) {
            stats.overruns++; reprime(write); return false;
        }
        if (!primed_) {
            if (available < target + history) return false;
            cursor_ = write - target; phase_ = 0; available = target; primed_ = true;
        }
        stats.queued = available;
        stats.min_queued = std::min(stats.min_queued, available); stats.max_queued = std::max(stats.max_queued, available);
        const double desired = std::clamp((double(available) - target) / target * 10000.0, -2000.0, 2000.0);
        stats.correction_ppm += std::clamp(desired - stats.correction_ppm, -5.0, 5.0);
        const double step = double(rate) / obs_rate * (1.0 + stats.correction_ppm / 1e6);
        const auto needed = uint32_t(std::floor(phase_ + double(frames - 1) * step)) + 2 + lookahead;
        if (available < needed) { stats.underruns++; reprime(write); return false; }
        bool good = true;
#ifdef AURALYN_SINC48_POC
        if (sinc_kind) {
            const size_t count = size_t(needed) + history;
            if (count > (sinc_kind == 1 ? Sinc48::cache_frames : Sinc96::cache_frames)) good = false;
            for (size_t i = 0; good && i < count; i++) {
                std::array<float, 2> value{};
                if (!frame(p, cursor_ - history + uint32_t(i), value)) { good = false; break; }
                if (sinc_kind == 1) { sinc_->left[i] = value[0]; sinc_->right[i] = value[1]; }
                else { sinc96_->left[i] = value[0]; sinc96_->right[i] = value[1]; }
            }
        }
#endif
        for (size_t i = 0; i < frames; i++) {
            if (!good) break;
#ifdef AURALYN_SINC48_POC
            if (sinc_kind) {
                if (sinc_kind == 1) sinc_->sample(phase_, left[i], right[i]);
                else sinc96_->sample(phase_, left[i], right[i]);
                phase_ += step; continue;
            }
#endif
            const auto integral = uint32_t(phase_);
            std::array<float, 2> a{}, b{};
            if (!frame(p, cursor_ + integral, a) || !frame(p, cursor_ + integral + 1, b)) { good = false; break; }
            const float fraction = float(phase_ - integral);
            // 有限な極値でも差分b-aをoverflowさせない。
            left[i] = std::lerp(a[0], b[0], fraction);
            right[i] = std::lerp(a[1], b[1], fraction);
            phase_ += step;
        }
        if (!good || load(p, 8) != epoch || load(p, 9) != 1 || load(p, 6) != rate) {
            std::fill_n(left, frames, 0.0f); std::fill_n(right, frames, 0.0f);
            if (!good) stats.invalid++;
            reprime(load(p, 10)); return false;
        }
        const auto advance = uint32_t(phase_); cursor_ += advance; phase_ -= advance;
        stats.blocks++; return true;
    }
};
}
