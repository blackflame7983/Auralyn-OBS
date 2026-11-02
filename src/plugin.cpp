#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <obs-module.h>
#include <util/platform.h>
#include "pcm.hpp"
#include "audio_clock.hpp"
#include <chrono>
#include <mutex>
#include <shared_mutex>
#include <string>
#include <thread>
#include <filesystem>
#include <fstream>

OBS_DECLARE_MODULE()
MODULE_EXPORT const char *obs_module_description(void) { return "Auralyn PCM source (local prototype, protocol 1.0)"; }

namespace {
bool valid_endpoint(const std::string &s) {
    return !s.empty() && s.size() <= 48 && std::all_of(s.begin(), s.end(), [](unsigned char c) {
        return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-';
    });
}
struct Source {
    std::shared_mutex mutex;
    std::string endpoint;
    std::atomic<bool> stop{false};
    std::thread worker;
    HANDLE mapping = nullptr, producer = nullptr;
    const uint32_t *memory = nullptr;
    auralyn::Reader reader;
    std::array<float, AUDIO_OUTPUT_FRAMES> left{}, right{};
    std::atomic<uint64_t> connections{0}, missed_locks{0}, clock_rebases{0};
    auralyn::BlockClock clock;
    std::filesystem::path telemetry_directory;
    uint64_t last_telemetry_ms=0;
    struct Telemetry {
        std::atomic<uint64_t> blocks{0}, underruns{0}, overruns{0}, invalid{0}, stale{0}, resets{0};
        std::atomic<uint32_t> queued{0}, min_queued{UINT32_MAX}, max_queued{0};
        std::atomic<double> correction_ppm{0};
        std::atomic<uint32_t> interpolation{0};
        void publish(const auralyn::Stats &s) noexcept {
            blocks.store(s.blocks); underruns.store(s.underruns); overruns.store(s.overruns);
            invalid.store(s.invalid); stale.store(s.stale); resets.store(s.resets);
            queued.store(s.queued); min_queued.store(s.min_queued); max_queued.store(s.max_queued);
            correction_ppm.store(s.correction_ppm);
            interpolation.store(s.interpolation);
        }
    } telemetry;
    static_assert(std::atomic<double>::is_always_lock_free);
    bool healthy() const {
        return memory && auralyn::load(memory, 9) != 2 &&
            WaitForSingleObject(producer, 0) == WAIT_TIMEOUT && auralyn::valid(memory);
    }
    void write_telemetry() {
        const auto now=GetTickCount64();
        if (telemetry_directory.empty() || now-last_telemetry_ms<1000) return;
        last_telemetry_ms=now;
        std::string name; uint32_t frame=0, rate=0, state=2;
        { std::shared_lock lock(mutex); name=endpoint;
          if(memory){frame=auralyn::load(memory,10);rate=auralyn::load(memory,6);state=auralyn::load(memory,9);} }
        if(!valid_endpoint(name)) return;
        const auto target=telemetry_directory/(name+".json"), pending=telemetry_directory/(name+".pending.json");
        std::ofstream out(pending);
        if(!out) return;
        out<<"{\"tickMs\":"<<now<<",\"blocks\":"<<telemetry.blocks.load()<<",\"underruns\":"<<telemetry.underruns.load()
           <<",\"overruns\":"<<telemetry.overruns.load()<<",\"invalid\":"<<telemetry.invalid.load()
           <<",\"stale\":"<<telemetry.stale.load()<<",\"resets\":"<<telemetry.resets.load()
           <<",\"connections\":"<<connections.load()<<",\"missedLocks\":"<<missed_locks.load()
           <<",\"clockRebases\":"<<clock_rebases.load()<<",\"queued\":"<<telemetry.queued.load()
           <<",\"minQueued\":"<<telemetry.min_queued.load()<<",\"maxQueued\":"<<telemetry.max_queued.load()
           <<",\"correctionPpm\":"<<telemetry.correction_ppm.load()<<",\"producerFrame\":"<<frame
           <<",\"producerRate\":"<<rate<<",\"state\":"<<state
           <<",\"interpolation\":\""<<(telemetry.interpolation.load() == 1 ? "sinc48-poc" : "linear-poc")<<"\"}\n";
        out.close();
        MoveFileExW(pending.c_str(),target.c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH);
    }
    void close() {
        reader.disconnect();
        if (memory) UnmapViewOfFile(memory);
        if (mapping) CloseHandle(mapping);
        if (producer) CloseHandle(producer);
        memory = nullptr; mapping = producer = nullptr;
    }
    void poll() {
        while (!stop.load()) {
            bool connected;
            { std::shared_lock lock(mutex); connected = healthy(); }
            if (!connected) {
                // 正常接続の監視はshared lockで音声読取と並行する。変更だけを排他にする。
                std::unique_lock lock(mutex);
                if (memory && !healthy()) close();
                if (!memory && valid_endpoint(endpoint)) {
                    std::wstring name = L"Local\\Auralyn.OBS.PoC.v1." + std::wstring(endpoint.begin(), endpoint.end());
                    mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, name.c_str());
                    if (mapping) {
                        memory = static_cast<const uint32_t *>(MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, auralyn::bytes));
                        if (memory && auralyn::valid(memory) && auralyn::load(memory, 9) != 2) producer = OpenProcess(SYNCHRONIZE, FALSE, auralyn::load(memory, 7));
                        if (!producer) close(); else { reader.disconnect(); connections++; }
                    }
                }
            }
            // 明示された検証先だけへ、非RTで観測値を保存する。
            write_telemetry();
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
    }
    ~Source() { stop.store(true); if (worker.joinable()) worker.join(); close(); }
};
void stats_proc(void *data, calldata_t *out) {
    auto &s = *static_cast<Source *>(data);
    const auto &v = s.telemetry;
    // 制御側の統計取得で音声threadを排他にしない。
    calldata_set_int(out, "blocks", v.blocks.load()); calldata_set_int(out, "underruns", v.underruns.load());
    calldata_set_int(out, "overruns", v.overruns.load()); calldata_set_int(out, "invalid", v.invalid.load());
    calldata_set_int(out, "stale", v.stale.load()); calldata_set_int(out, "resets", v.resets.load());
    calldata_set_int(out, "connections", s.connections.load()); calldata_set_int(out, "queued", v.queued.load());
    calldata_set_int(out, "min_queued", v.min_queued.load()); calldata_set_int(out, "max_queued", v.max_queued.load());
    calldata_set_float(out, "correction_ppm", v.correction_ppm.load());
    calldata_set_int(out, "missed_locks", s.missed_locks.load());
    calldata_set_int(out, "clock_rebases", s.clock_rebases.load());
}
void *create(obs_data_t *settings, obs_source_t *source) {
    try {
        auto s = std::make_unique<Source>(); s->endpoint = obs_data_get_string(settings, "endpoint");
        if(const auto folder=_wgetenv(L"AURALYN_OBS_POC_TELEMETRY_DIR")) {
            std::error_code error;
            if(std::filesystem::is_directory(folder,error)) s->telemetry_directory=folder;
        }
        proc_handler_add(obs_source_get_proc_handler(source), "void get_bridge_stats()", stats_proc, s.get());
        s->worker = std::thread([ptr = s.get()] { ptr->poll(); }); return s.release();
    } catch (...) { blog(LOG_ERROR, "[Auralyn PoC] ソースを作成できません"); return nullptr; }
}
void update(void *data, obs_data_t *settings) {
    auto &s = *static_cast<Source *>(data); std::lock_guard lock(s.mutex);
    std::string endpoint = obs_data_get_string(settings, "endpoint");
    if (s.endpoint != endpoint) { s.close(); s.endpoint = std::move(endpoint); }
}
obs_properties_t *properties(void *) {
    auto p = obs_properties_create(); obs_properties_add_text(p, "endpoint", "送信側と同じ接続名（試作）", OBS_TEXT_DEFAULT); return p;
}
bool render(void *data, uint64_t *ts, audio_output_data *audio, size_t channels, size_t rate) {
    if (!rate || channels > MAX_AUDIO_CHANNELS) return false;
    auto &s = *static_cast<Source *>(data);
    const auto now = os_gettime_ns();
    const auto previous_rebases = s.clock.rebases;
    *ts = s.clock.advance(AUDIO_OUTPUT_FRAMES, uint32_t(rate), now);
    s.clock_rebases.store(s.clock.rebases);
    // 接続の変更中にも無音blockを返してclockを進める。OBSへ古い時刻を渡さない。
    std::shared_lock lock(s.mutex, std::try_to_lock);
    if (lock.owns_lock()) {
        if (previous_rebases != s.clock.rebases) s.reader.disconnect();
        s.reader.render(s.memory, s.left.data(), s.right.data(), AUDIO_OUTPUT_FRAMES, uint32_t(rate), now);
        s.telemetry.publish(s.reader.stats);
    } else {
        s.missed_locks++;
        s.left.fill(0); s.right.fill(0);
    }
    for (size_t ch = 0; ch < channels; ch++) {
            auto out = audio->data[ch];
            if (channels == 1) for (size_t i = 0; i < AUDIO_OUTPUT_FRAMES; i++) out[i] = auralyn::mono(s.left[i], s.right[i]);
            else if (ch < 2) std::copy_n(ch == 0 ? s.left.data() : s.right.data(), AUDIO_OUTPUT_FRAMES, out);
            else std::fill_n(out, AUDIO_OUTPUT_FRAMES, 0.0f);
    }
    return true;
}
}
bool obs_module_load(void) {
    if (obs_get_version() != LIBOBS_API_VER) return false;
    obs_source_info info{};
    info.id = "auralyn_pcm_poc_v1"; info.type = OBS_SOURCE_TYPE_INPUT;
    info.output_flags = OBS_SOURCE_AUDIO | OBS_SOURCE_DO_NOT_DUPLICATE;
    info.get_name = [](void *) { return "Auralyn PCM（試作）"; };
    info.create = create; info.destroy = [](void *p) { delete static_cast<Source *>(p); };
    // audio_mixはOBSの周期で呼ばれ、libobs自身の音声queue・音量・filterへ渡る。
    info.update = update; info.get_properties = properties; info.audio_mix = render;
    info.icon_type = OBS_ICON_TYPE_AUDIO_INPUT;
    obs_register_source(&info); return true;
}
