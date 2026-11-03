#include "pcm.hpp"
#include "audio_clock.hpp"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <limits>
#include <regex>
#include <sstream>
#include <stdexcept>
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
        data[2] = (rate == 88200 || rate == 176400 || rate == 192000) ? 1 : 0;
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
    assert(number(json, "major") == 1 && number(json, "minor") == 1 && number(json, "legacyMinor") == 0);
    assert(number(json, "headerWords") == auralyn::header_words && number(json, "slotWords") == auralyn::slot_words);
    std::regex rate_row("\\{[^{}]*\"rate\"[^{}]*\"streamMinor\"[^{}]*\\}");
    int rates = 0;
    for (auto it = std::sregex_iterator(json.begin(), json.end(), rate_row); it != std::sregex_iterator(); ++it) {
        auto item = it->str();
        const auto rate = uint32_t(number(item, "rate"));
        Producer producer(rate);
        assert(number(item, "streamMinor") == producer.data[2]);
        assert(auralyn::valid(producer.data.data()));
        rates++;
    }
    assert(rates == 6);
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
    for (uint32_t rate : {44100u, 48000u, 96000u, 88200u, 176400u, 192000u, 384000u}) {
        Producer stream(rate);
        for (uint32_t minor : {0u, 1u, 2u}) {
            stream.data[2] = minor;
            const bool expected = minor <= 1 && (rate == 44100 || rate == 48000 || rate == 96000 ||
                (minor == 1 && (rate == 88200 || rate == 176400 || rate == 192000)));
            assert(auralyn::valid(stream.data.data()) == expected);
            if (!expected) {
                assert(!reader.render(stream.data.data(), l.data(), r.data(), l.size(), 48000, 0));
                for (float value : l) assert(value == 0);
            }
        }
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

// 実Readerへ同じデジタル入力を渡すオフライン測定。実時間・実機・聴感の受入ではない。
void quality(const std::filesystem::path &input, const std::filesystem::path &output, double ppm, uint32_t input_rate) {
    if (input_rate != 48000 && input_rate != 88200 && input_rate != 96000 && input_rate != 176400 && input_rate != 192000) throw std::runtime_error("input rate must be 48000, 88200, 96000, 176400 or 192000");
    if (!std::isfinite(ppm) || std::abs(ppm) > 1000) throw std::runtime_error("ppm must be finite and within +/-1000");
    std::ifstream source(input, std::ios::binary | std::ios::ate);
    if (!source) throw std::runtime_error("cannot open input f32le");
    const auto length = std::streamoff(source.tellg());
    if (length < 4LL * input_rate * 8 || length > 300LL * input_rate * 8 || length % 8 != 0)
        throw std::runtime_error("input must contain 4..300 seconds of stereo input-rate f32le");
    std::vector<float> raw(size_t(length) / sizeof(float));
    source.seekg(0); source.read(reinterpret_cast<char *>(raw.data()), std::streamsize(length));
    if (!source || !std::all_of(raw.begin(), raw.end(), [](float x) { return std::isfinite(x); }))
        throw std::runtime_error("invalid input samples");
    // 入力の検証後に一度だけ作る。既存の測定を上書きしない。
    if (!std::filesystem::create_directory(output)) throw std::runtime_error("output directory already exists");
    auto write_raw = [&](const char *name, const std::vector<float> &samples) {
        std::ofstream file(output / name, std::ios::binary);
        file.write(reinterpret_cast<const char *>(samples.data()), std::streamsize(samples.size() * sizeof(float)));
        if (!file) throw std::runtime_error("cannot write raw measurement");
    };
    write_raw("raw-input.f32", raw);
    Producer producer(input_rate); auralyn::Reader reader; std::array<float, 1024> left{}, right{};
    std::vector<float> rendered; rendered.reserve(raw.size() + 4096);
    std::ofstream timeline(output / "timeline.jsonl");
    if (!timeline) throw std::runtime_error("cannot write timeline");
    reader.render(producer.data.data(), left.data(), right.data(), 1024, 48000, 0);
    uint64_t written = 0, prefill = 0, unavailable = 0, skipped_at_start = 0, next_log = 48000;
    double produced = 0; bool started = false;
    for (uint64_t block = 1;; block++) {
        produced += 1024.0 * input_rate / 48000 * (1 + ppm / 1e6);
        const auto total = uint64_t(produced);
        if (total > raw.size() / 2) break; // 入力末尾を無音でflushしない。
        for (; written < total; written++) {
            uint32_t l, r;
            std::memcpy(&l, &raw[size_t(written) * 2], 4);
            std::memcpy(&r, &raw[size_t(written) * 2 + 1], 4);
            producer.push(l, r);
        }
        const auto before = allocations;
        const bool good = reader.render(producer.data.data(), left.data(), right.data(), 1024, 48000,
                                        block * 1024 * 1000000000 / 48000);
        assert(allocations == before);
        if (good && !started) { skipped_at_start = written - input_rate / 20; started = true; }
        if (!good) { if (!started) prefill += 1024; else unavailable += 1024; }
        for (size_t i = 0; i < 1024; i++) { rendered.push_back(left[i]); rendered.push_back(right[i]); }
        if (block * 1024 >= next_log) {
            timeline << "{\"seconds\":" << double(block * 1024) / 48000
                     << ",\"queuedFrames\":" << reader.stats.queued << ",\"correctionPpm\":" << reader.stats.correction_ppm << "}\n";
            next_log += 48000;
        }
    }
    write_raw("after.f32", rendered);
    const auto &s = reader.stats;
    std::ofstream stats(output / "stats.json");
    stats.precision(12);
    stats << "{\"model\":\"offline actual OBS Reader; not physical devices or real-time performance\","
          << "\"sampleRateHz\":48000,\"inputSampleRateHz\":" << input_rate << ",\"outputSampleRateHz\":48000,\"channels\":2,\"ppm\":" << ppm
          << ",\"inputFrames\":" << raw.size() / 2 << ",\"outputFrames\":" << rendered.size() / 2
          << ",\"prefillFrames\":" << prefill << ",\"startupSourceSkippedFrames\":" << skipped_at_start
          << ",\"unavailableFramesAfterStart\":" << unavailable << ",\"blocks\":" << s.blocks
          << ",\"underruns\":" << s.underruns << ",\"overruns\":" << s.overruns
          << ",\"invalid\":" << s.invalid << ",\"stale\":" << s.stale << ",\"resets\":" << s.resets
          << ",\"correctionPpm\":" << s.correction_ppm
          << ",\"interpolation\":\"" << auralyn::interpolation_name(s.interpolation) << "\",\"flushRecorded\":false}\n";
    timeline.flush(); stats.flush();
    if (!timeline || !stats || !started || unavailable || s.underruns || s.overruns || s.invalid || s.stale)
        throw std::runtime_error("offline transport measurement failed; retain evidence");
}

void sinc_boundaries() {
    assert(std::isfinite(auralyn::mono(std::numeric_limits<float>::max(), std::numeric_limits<float>::max())));
#ifdef AURALYN_SINC48_POC
    for (uint32_t rate : {48000u, 96000u, 88200u, 176400u, 192000u}) {
        const uint32_t written = rate / 20 + 1000;
        for (double correction : {-2000.0, 2000.0}) {
            Producer p(rate); auralyn::Reader reader; std::array<float, 2048> l{}, r{};
            reader.render(p.data.data(), l.data(), r.data(), l.size(), 48000, 0);
            for (size_t i = 0; i < written; i++) p.push(i % 2 ? 0x7f7fffff : 0xff7fffff, 0x7f7fffff);
            reader.stats.correction_ppm = correction;
            const auto count = allocations;
            assert(reader.render(p.data.data(), l.data(), r.data(), l.size(), 48000, 10000000));
            assert(allocations == count && reader.stats.interpolation == (rate == 48000 ? 1u : rate == 96000 ? 2u : rate == 88200 ? 3u : rate == 176400 ? 4u : 5u));
            for (size_t i = 0; i < l.size(); i++) assert(std::isfinite(l[i]) && std::isfinite(r[i]));
        }
        Producer bad(rate); auralyn::Reader reader; std::array<float, 1024> l{}, r{};
        reader.render(bad.data.data(), l.data(), r.data(), l.size(), 48000, 0);
        bad.fill(written);
        // 出力開始位置より前のhistoryも、未完了tagを受け入れない。
        const uint32_t history = rate == 48000 ? 63 : rate <= 96000 ? 127 : 255;
        bad.data[16 + (written - rate / 20 - history) * 3] |= 1;
        assert(!reader.render(bad.data.data(), l.data(), r.data(), l.size(), 48000, 10000000));
        assert(reader.stats.invalid == 1);
        for (float value : l) assert(value == 0);
    }
    // 96k入力の係数による周波数応答。phase補間の端と中間も確認する。
    const auto bank = std::make_unique<auralyn::Sinc96>();
    for (size_t phase : {size_t(0), size_t(128), size_t(256)}) {
        for (double frequency : {100.0, 1000.0, 10000.0, 19000.0, 20000.0, 24000.0, 25000.0}) {
            double re = 0, im = 0;
            for (size_t k = 0; k < bank->taps; k++) {
                const double angle = 2 * std::numbers::pi * frequency * double(k) / 96000;
                re += bank->coefficients[phase][k] * std::cos(angle);
                im += bank->coefficients[phase][k] * std::sin(angle);
            }
            const double gain = std::hypot(re, im);
            if (frequency <= 20000) assert(gain > .999 && gain < 1.001);
            else assert(gain < 0.0001);
        }
    }
    // 新バンクも可聴帯域と折返し帯域の応答を検証する。
    auto response = [](const auto &bank, double rate) {
        for (size_t phase : {size_t(0), size_t(128), size_t(256)}) {
            for (double frequency : {100.0, 1000.0, 10000.0, 19000.0, 20000.0, 24000.0, 25000.0}) {
                double re = 0, im = 0;
                for (size_t k = 0; k < bank.taps; k++) {
                    const double angle = 2 * std::numbers::pi * frequency * double(k) / rate;
                    re += bank.coefficients[phase][k] * std::cos(angle);
                    im += bank.coefficients[phase][k] * std::sin(angle);
                }
                const double gain = std::hypot(re, im);
                if (frequency <= 20000) assert(gain > .999 && gain < 1.001);
                else assert(gain < .0001);
            }
        }
    };
    response(*std::make_unique<auralyn::Sinc88>(), 88200);
    response(*std::make_unique<auralyn::Sinc176>(), 176400);
    response(*std::make_unique<auralyn::Sinc192>(), 192000);
    // epochが変化しないレート切替でも旧履歴を使わず再primeする。
    Producer switched; auralyn::Reader changing; std::array<float, 1024> l{}, r{};
    assert(!changing.render(switched.data.data(), l.data(), r.data(), l.size(), 48000, 0));
    switched.fill(3000); assert(changing.render(switched.data.data(), l.data(), r.data(), l.size(), 48000, 10000000));
    switched.data[6] = 96000;
    assert(!changing.render(switched.data.data(), l.data(), r.data(), l.size(), 48000, 20000000));
    switched.fill(6000); assert(changing.render(switched.data.data(), l.data(), r.data(), l.size(), 48000, 30000000));
    assert(changing.stats.interpolation == 2 && changing.stats.resets == 2);
    assert(!changing.render(switched.data.data(), l.data(), r.data(), l.size(), 44100, 40000000));
    switched.fill(6000); assert(changing.render(switched.data.data(), l.data(), r.data(), l.size(), 44100, 50000000));
    assert(changing.stats.interpolation == 0 && changing.stats.resets == 3);
    switched.data[8]++;
    assert(!changing.render(switched.data.data(), l.data(), r.data(), l.size(), 48000, 60000000));
    switched.fill(6000); assert(changing.render(switched.data.data(), l.data(), r.data(), l.size(), 48000, 70000000));
    assert(changing.stats.resets == 4 && changing.stats.interpolation == 2);
    for (uint32_t rate : {88200u, 176400u, 192000u}) {
        switched.data[2] = 1; switched.data[6] = rate;
        assert(!changing.render(switched.data.data(), l.data(), r.data(), l.size(), 48000, 80000000));
        switched.fill(rate / 20 + 1000);
        assert(changing.render(switched.data.data(), l.data(), r.data(), l.size(), 48000, 90000000));
        assert(changing.stats.interpolation == (rate == 88200 ? 3u : rate == 176400 ? 4u : 5u));
    }
#endif
}

int main(int argc, char **argv) {
    if ((argc == 5 || argc == 6) && std::string(argv[1]) == "--quality") {
        try {
            size_t parsed = 0; const std::string value(argv[4]); const double ppm = std::stod(value, &parsed);
            if (parsed != value.size()) throw std::runtime_error("invalid ppm argument");
            uint32_t rate = 48000;
            if (argc == 6) {
                size_t rate_parsed = 0; const std::string text(argv[5]);
                const auto value_rate = std::stoul(text, &rate_parsed);
                if (rate_parsed != text.size() || (value_rate != 48000 && value_rate != 88200 && value_rate != 96000 && value_rate != 176400 && value_rate != 192000)) throw std::runtime_error("input rate must be 48000, 88200, 96000, 176400 or 192000");
                rate = uint32_t(value_rate);
            }
            quality(argv[2], argv[3], ppm, rate); return 0;
        } catch (const std::exception &error) { std::cerr << error.what() << '\n'; return 1; }
    }
    if (argc != 1 && !(argc == 2 && std::string(argv[1]) == "--long")) {
        std::cerr << "Usage: pcm_tests [--long | --quality input.f32 new-output-directory ppm [input-rate]]\n"; return 2;
    }
    const size_t seconds = argc == 2 && std::string(argv[1]) == "--long" ? 3600 : 120;
    fixture(); failures(); sinc_boundaries();
    for (uint32_t rate : {44100u,48000u,96000u,88200u,176400u,192000u}) {
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
    for (uint32_t rate : {44100u,48000u,96000u,88200u,176400u,192000u}) { clocks(rate, -1000, seconds); clocks(rate, 1000, seconds); }
    std::cout << "PCM contract, mute, stale, wrap, overwrite, malformed headers and allocation checks passed\n";
}
