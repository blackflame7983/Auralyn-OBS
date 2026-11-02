#include "pcm.hpp"
#include "audio_clock.hpp"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <regex>
#include <sstream>
#include <vector>

namespace {
size_t allocations = 0;
}
void *operator new(size_t n) { allocations++; if (auto p = std::malloc(n)) return p; throw std::bad_alloc(); }
void operator delete(void *p) noexcept { std::free(p); }
void operator delete(void *p, size_t) noexcept { std::free(p); }

struct Producer {
    std::vector<uint32_t> data = std::vector<uint32_t>(auralyn::words);
    uint32_t cursor = 0;
    explicit Producer(uint32_t rate = 48000) {
        data[0] = auralyn::magic; data[1] = 1; data[3] = auralyn::bytes;
        data[4] = auralyn::capacity; data[5] = 2; data[6] = rate; data[7] = 1;
        data[8] = 1; data[9] = 1;
        for (size_t i = 0; i < auralyn::capacity; i++) data[16 + i * 3] = UINT32_MAX;
    }
    void push(uint32_t left, uint32_t right) {
        auto index = 16 + (cursor & 16383) * 3;
        auto put = [&](size_t n, uint32_t v) { std::atomic_ref<uint32_t>(data[n]).store(v); };
        put(index, (cursor << 1) | 1); put(index + 1, left); put(index + 2, right);
        put(index, cursor << 1); cursor++; put(10, cursor);
    }
    void fill(uint32_t n) { for (uint32_t i = 0; i < n; i++) push(0x3d4ccccd, 0xbd4ccccd); }
};
uint64_t number(const std::string &json, const std::string &name) {
    std::smatch match; assert(std::regex_search(json, match, std::regex("\"" + name + "\"\\s*:\\s*([0-9]+)")));
    return std::stoull(match[1]);
}
void fixture() {
    std::ifstream stream(FIXTURE_PATH); assert(stream);
    std::stringstream text; text << stream.rdbuf(); auto json = text.str();
    assert(number(json, "magic") == auralyn::magic);
    assert(number(json, "mappingBytes") == auralyn::bytes);
    assert(number(json, "capacity") == auralyn::capacity);
    assert(number(json, "major") == 1 && number(json, "minor") == 0);
    assert(number(json, "headerWords") == auralyn::header_words && number(json, "slotWords") == auralyn::slot_words);
    std::regex row("\\{[^{}]*\"frame\"[^{}]*\\}");
    int rows = 0;
    for (auto it = std::sregex_iterator(json.begin(), json.end(), row); it != std::sregex_iterator(); ++it) {
        auto item = it->str(); Producer p; p.cursor = uint32_t(number(item, "frame"));
        p.push(uint32_t(number(item, "leftBits")), uint32_t(number(item, "rightBits")));
        const auto index = number(item, "slotWord");
        assert(p.data[index] == number(item, "tag"));
        assert(p.data[index + 1] == number(item, "leftBits") && p.data[index + 2] == number(item, "rightBits")); rows++;
    }
    assert(rows == 3);
}
void failures() {
    Producer p; auralyn::Reader reader; std::array<float, 1024> l{}, r{};
    auto render = [&](uint64_t now) { const auto before = allocations; const bool ok = reader.render(p.data.data(), l.data(), r.data(), l.size(), 48000, now); assert(allocations == before); return ok; };
    assert(!render(0)); p.fill(3000); assert(render(10000000)); assert(l[0] > 0 && r[0] < 0);
    p.data[9] = 0; p.data[8]++; assert(!render(20000000)); for (float x : l) assert(x == 0);
    p.data[9] = 1; assert(!render(30000000)); p.fill(3000); assert(render(40000000));
    assert(!render(200000001)); assert(reader.stats.stale == 1);
    p.fill(20000); assert(!render(210000000)); assert(reader.stats.overruns == 1);
    p.fill(3000); assert(render(220000000));
    // 未完了tagは音として読まない。
    for (size_t i = 16; i < p.data.size(); i += 3) p.data[i] |= 1;
    assert(!render(230000000)); assert(reader.stats.invalid > 0);
    p.data[9] = 2; assert(!render(240000000));
    for (size_t index : {size_t(0),size_t(1),size_t(2),size_t(3),size_t(4),size_t(5),size_t(6),size_t(7)}) {
        Producer bad; bad.data[index] = index == 7 ? 0 : UINT32_MAX; assert(!auralyn::valid(bad.data.data()));
    }
    Producer wrap; wrap.cursor = UINT32_MAX - 1500; wrap.data[10] = wrap.cursor;
    auralyn::Reader wrapped;
    assert(!wrapped.render(wrap.data.data(), l.data(), r.data(), 1024, 48000, 0));
    wrap.fill(4000); assert(wrapped.render(wrap.data.data(), l.data(), r.data(), 1024, 48000, 20000000));
    Producer extremes(44100); auralyn::Reader bounded;
    assert(!bounded.render(extremes.data.data(), l.data(), r.data(), 1024, 48000, 0));
    for (size_t i = 0; i < 3000; i++) {
        const uint32_t bits = i % 4 == 0 ? 0x7fc00000 : (i % 4 == 1 ? 0x7f800000 : (i % 4 == 2 ? 0x7f7fffff : 0xff7fffff));
        extremes.push(bits, bits);
    }
    assert(bounded.render(extremes.data.data(), l.data(), r.data(), 1024, 48000, 20000000));
    for (float value : l) assert(std::isfinite(value));
}
void clocks(uint32_t rate, double ppm, size_t seconds) {
    Producer p(rate); auralyn::Reader reader; std::array<float, 1024> l{}, r{};
    constexpr uint32_t obs_rate = 48000;
    double produced = 0; uint64_t written = 0;
    const size_t blocks = seconds * obs_rate / 1024;
    reader.render(p.data.data(), l.data(), r.data(), 1024, obs_rate, 0);
    const auto before = allocations;
    for (size_t i = 1; i <= blocks; i++) {
        produced += 1024.0 * rate / obs_rate * (1 + ppm / 1e6);
        const uint64_t total = uint64_t(produced);
        p.fill(uint32_t(total - written)); written = total;
        reader.render(p.data.data(), l.data(), r.data(), 1024, obs_rate, uint64_t(double(i) * 1024 / obs_rate * 1e9));
    }
    assert(allocations == before);
    std::cout << "stats: blocks=" << reader.stats.blocks << " underruns=" << reader.stats.underruns << " overruns=" << reader.stats.overruns << " invalid=" << reader.stats.invalid << " stale=" << reader.stats.stale << std::endl;
    assert(reader.stats.blocks > blocks - 10);
    assert(reader.stats.underruns == 0 && reader.stats.overruns == 0 && reader.stats.invalid == 0 && reader.stats.stale == 0);
    assert(std::abs(reader.stats.correction_ppm) <= 2000);
    assert(reader.stats.min_queued > 1024 * rate / obs_rate);
    assert(reader.stats.max_queued < rate / 10);
    std::cout << "clock rate=" << rate << " ppm=" << ppm << " queue=" << reader.stats.min_queued << ".." << reader.stats.max_queued << " correction=" << reader.stats.correction_ppm << " blocks=" << reader.stats.blocks << '\n';
}
int main(int argc, char **argv) {
    const size_t seconds = argc == 2 && std::string(argv[1]) == "--long" ? 3600 : 120;
    fixture(); failures();
    for (uint32_t rate : {44100u,48000u,96000u}) {
        auralyn::BlockClock clock;
        const uint64_t begin=10000000000, first=clock.advance(1024,rate,begin);
        const auto allocations_before=allocations;
        for (uint64_t i=1; i<200000; i++) {
            const auto delta=i*1024*1000000000/rate;
            // 7回の読取失敗を含め、全callbackでclockを進める。
            const auto ts=clock.advance(1024,rate,begin+delta);
            assert(ts==first+delta);
        }
        assert(allocations==allocations_before && clock.rebases==1);
        clock.advance(1024,rate,10000000000000);
        assert(clock.rebases==2);
    }
    for (uint32_t rate : {44100u,48000u,96000u}) { clocks(rate, -1000, seconds); clocks(rate, 1000, seconds); }
    std::cout << "PCM contract, mute, stale, wrap, overwrite, malformed headers and allocation checks passed\n";
}
