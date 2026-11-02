#pragma once
#include <cstdint>

namespace auralyn {
// 読取成否から独立したOBSのblock clock。欠けた読取を時刻の遅れに変えない。
class BlockClock {
    uint64_t next_ = 0, remainder_ = 0;
    uint32_t rate_ = 0;
public:
    uint64_t rebases = 0;
    uint64_t advance(uint32_t frames, uint32_t rate, uint64_t now) noexcept {
        const uint64_t duration = uint64_t(frames) * 1000000000;
        if (!next_ || rate != rate_ || now > next_ + 200000000 || next_ > now + 200000000) {
            next_ = now - duration / rate; remainder_ = 0; rate_ = rate; rebases++;
        }
        const uint64_t timestamp = next_;
        const uint64_t interval = duration + remainder_;
        next_ += interval / rate; remainder_ = interval % rate;
        return timestamp;
    }
};
}
