#include <obs.h>
#include <util/platform.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <thread>

struct Capture { std::atomic<uint64_t> blocks{0}, nonzero{0}, quiet_tail{0}, bad_stereo{0}; };
static void capture(void *ptr, size_t, audio_data *audio) {
    auto &s = *static_cast<Capture *>(ptr);
    const auto l = reinterpret_cast<const float *>(audio->data[0]), r = reinterpret_cast<const float *>(audio->data[1]);
    bool signal = false, bad = false;
    for (size_t i = 0; i < audio->frames; i++) {
        if (std::abs(l[i]) > 0.001f) signal = true;
        if (std::abs(l[i] + r[i]) > 0.00001f || !std::isfinite(l[i]) || !std::isfinite(r[i])) bad = true;
    }
    s.blocks++; if (signal) { s.nonzero++; s.quiet_tail.store(0); } else s.quiet_tail++;
    if (bad) s.bad_stereo++;
}
int main(int argc, char **argv) {
    if (argc != 7 && argc != 8) { std::fprintf(stderr, "usage: obs_smoke PLUGIN_DLL DATA_DIR ENDPOINT SECONDS MIN_CONNECTIONS OBS_BIN_DIR [stress-stats]\n"); return 2; }
    const double seconds = std::strtod(argv[4], nullptr); const int connections = std::atoi(argv[5]);
    if (!(seconds >= 1 && seconds <= 7200 && connections >= 1)) return 2;
    if (!obs_startup("en-US", nullptr, nullptr)) return 3;
    const std::string runtime = argv[6], graphics = runtime + "/libobs-d3d11.dll", assets = runtime + "/../../data/libobs/";
    obs_add_data_path(assets.c_str());
    // libobsの最終mixにはvideo viewが必要。画面・設定・録画を作らない32pxの隔離view。
    obs_video_info video{};
    video.graphics_module = graphics.c_str(); video.fps_num = 30; video.fps_den = 1;
    video.base_width = video.output_width = 32; video.base_height = video.output_height = 32;
    video.output_format = VIDEO_FORMAT_RGBA; video.colorspace = VIDEO_CS_709;
    video.range = VIDEO_RANGE_FULL; video.scale_type = OBS_SCALE_BILINEAR;
    if (obs_reset_video(&video) != OBS_VIDEO_SUCCESS) { obs_shutdown(); return 8; }
    obs_audio_info info{48000, SPEAKERS_STEREO};
    if (!obs_reset_audio(&info)) { obs_shutdown(); return 4; }
    obs_module_t *module = nullptr;
    if (obs_open_module(&module, argv[1], argv[2]) != MODULE_SUCCESS || !obs_init_module(module)) { obs_shutdown(); return 5; }
    auto settings = obs_data_create(); obs_data_set_string(settings, "endpoint", argv[3]);
    auto source = obs_source_create("auralyn_pcm_poc_v1", "Auralyn isolated PCM smoke", settings, nullptr);
    obs_data_release(settings);
    if (!source) { obs_shutdown(); return 6; }
    obs_set_output_source(0, source);
    Capture state;
    audio_convert_info conversion{48000, AUDIO_FORMAT_FLOAT_PLANAR, SPEAKERS_STEREO};
    obs_add_raw_audio_callback(0, &conversion, capture, &state);
    std::puts("AURALYN_SMOKE_READY"); std::fflush(stdout);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::duration<double>(seconds);
    while (std::chrono::steady_clock::now() < deadline) {
        if (argc == 8) for (int i=0; i<128; ++i) {
            calldata_t sample; calldata_init(&sample);
            proc_handler_call(obs_source_get_proc_handler(source), "get_bridge_stats", &sample);
            calldata_free(&sample);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    calldata_t stats; calldata_init(&stats);
    const bool queried = proc_handler_call(obs_source_get_proc_handler(source), "get_bridge_stats", &stats);
    const auto blocks = calldata_int(&stats,"blocks"), conns = calldata_int(&stats,"connections");
    const auto underruns = calldata_int(&stats,"underruns"), overruns = calldata_int(&stats,"overruns"), invalid = calldata_int(&stats,"invalid");
    std::printf("clock_diagnostics missed_locks=%lld resets=%lld stale=%lld\n", calldata_int(&stats,"missed_locks"),calldata_int(&stats,"resets"),calldata_int(&stats,"stale"));
    std::printf("{\"captureBlocks\":%llu,\"nonzeroBlocks\":%llu,\"quietTail\":%llu,\"badStereo\":%llu,\"sourceBlocks\":%lld,\"connections\":%lld,\"underruns\":%lld,\"overruns\":%lld,\"invalid\":%lld,\"queueMin\":%lld,\"queueMax\":%lld,\"correctionPpm\":%.3f}\n",
        state.blocks.load(),state.nonzero.load(),state.quiet_tail.load(),state.bad_stereo.load(),blocks,conns,underruns,overruns,invalid,
        calldata_int(&stats,"min_queued"),calldata_int(&stats,"max_queued"),calldata_float(&stats,"correction_ppm"));
    const bool pass = queried && state.nonzero > 300 && state.quiet_tail >= 10 && state.bad_stereo == 0 && blocks > 300 && conns >= connections && underruns == 0 && overruns == 0 && invalid == 0;
    calldata_free(&stats);
    obs_remove_raw_audio_callback(0, capture, &state); obs_set_output_source(0, nullptr); obs_source_release(source); obs_shutdown();
    return pass ? 0 : 7;
}
