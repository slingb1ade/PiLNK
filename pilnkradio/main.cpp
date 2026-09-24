// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * pilnkradio - standalone PiLNK radio daemon (v2 engine).
 *   librtlsdr direct + AM demod + HTTP/WebSocket control API on one port.
 *   Serves the exact same :5656 wire contract as the pilnk_bridge SDR++
 *   module it replaces, so the SDR Audio tab is unchanged.
 *
 * Copyright (C) 2026 AJ McLachlan / PiLNK
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version. (Links librtlsdr and FFTW, both
 * GPL; the daemon is GPL-3.0-or-later.)
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU General Public License for more details.
 *
 * Server code (HTTP/WS, broadcast, hardening) ported verbatim from
 * pilnk_bridge/src/main.cpp @ 5aefe59 — audit fixes H2/M1/M2 carried over.
 */
#include "json.hpp"
#include <rtl-sdr.h>
#include <fftw3.h>

#include <cmath>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cerrno>
#include <csignal>
#include <cstdarg>
#include <ctime>

#include <thread>
#include <mutex>
#include <atomic>
#include <chrono>
#include <deque>
#include <vector>
#include <string>
#include <fstream>
#include <algorithm>
#include <condition_variable>

#include <sys/socket.h>
#include <sys/time.h>
#include <sys/stat.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <dlfcn.h>        // v2.1.0: optional DeepFilterNet (loaded at runtime, never linked)
#include <sys/wait.h>
#include <memory>

using nlohmann::json;

#define PILNKRADIO_VERSION "2.1.0"
#define PILNK_FFT_SIZE     1024
#define PILNK_FFT_RATE     25.0

// ======================= logging =======================
// stdout, one line per event; systemd journal adds persistence.
static void logLine(const char* lvl, const char* fmt, ...) {
    char msg[1024];
    va_list ap; va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    char ts[32];
    time_t t = time(nullptr);
    struct tm tmv; localtime_r(&t, &tmv);
    strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", &tmv);
    fprintf(stdout, "[%s] [%s] %s\n", ts, lvl, msg);
    fflush(stdout);
}
#define logI(...) logLine("INF", __VA_ARGS__)
#define logW(...) logLine("WRN", __VA_ARGS__)
#define logE(...) logLine("ERR", __VA_ARGS__)

// ======================= small crypto helpers (WS handshake) =======================
// (ported verbatim from pilnk_bridge)
namespace pilnk {
    static const char* B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    static std::string base64(const uint8_t* d, size_t n) {
        std::string o; int v = 0, b = -6;
        for (size_t i = 0; i < n; i++) {
            v = (v << 8) + d[i]; b += 8;
            while (b >= 0) { o.push_back(B64[(v >> b) & 0x3F]); b -= 6; }
        }
        if (b > -6) o.push_back(B64[((v << 8) >> (b + 8)) & 0x3F]);
        while (o.size() % 4) o.push_back('=');
        return o;
    }
    // minimal SHA-1
    static void sha1(const uint8_t* msg, size_t len, uint8_t out[20]) {
        uint32_t h0=0x67452301,h1=0xEFCDAB89,h2=0x98BADCFE,h3=0x10325476,h4=0xC3D2E1F0;
        size_t ml = len * 8;
        std::vector<uint8_t> m(msg, msg + len);
        m.push_back(0x80);
        while (m.size() % 64 != 56) m.push_back(0);
        for (int i = 7; i >= 0; i--) m.push_back((ml >> (i * 8)) & 0xFF);
        auto rol = [](uint32_t x, int c){ return (x << c) | (x >> (32 - c)); };
        for (size_t off = 0; off < m.size(); off += 64) {
            uint32_t w[80];
            for (int i = 0; i < 16; i++)
                w[i] = (m[off+i*4]<<24)|(m[off+i*4+1]<<16)|(m[off+i*4+2]<<8)|m[off+i*4+3];
            for (int i = 16; i < 80; i++) w[i] = rol(w[i-3]^w[i-8]^w[i-14]^w[i-16], 1);
            uint32_t a=h0,b=h1,c=h2,d=h3,e=h4;
            for (int i = 0; i < 80; i++) {
                uint32_t f,k;
                if (i<20){f=(b&c)|((~b)&d);k=0x5A827999;}
                else if (i<40){f=b^c^d;k=0x6ED9EBA1;}
                else if (i<60){f=(b&c)|(b&d)|(c&d);k=0x8F1BBCDC;}
                else {f=b^c^d;k=0xCA62C1D6;}
                uint32_t t = rol(a,5)+f+e+k+w[i];
                e=d; d=c; c=rol(b,30); b=a; a=t;
            }
            h0+=a; h1+=b; h2+=c; h3+=d; h4+=e;
        }
        uint32_t hh[5]={h0,h1,h2,h3,h4};
        for (int i = 0; i < 5; i++) { out[i*4]=hh[i]>>24; out[i*4+1]=hh[i]>>16; out[i*4+2]=hh[i]>>8; out[i*4+3]=hh[i]; }
    }
    static std::string wsAccept(const std::string& key) {
        std::string s = key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
        uint8_t dig[20]; sha1((const uint8_t*)s.data(), s.size(), dig);
        return base64(dig, 20);
    }
}

// ======================= mode mapping =======================
// Same strings as the bridge API. v2.0 implements AM only (all the fleet
// needs for airband); other known modes are recognized but rejected with 400
// at the route layer so the error is "unsupported", not "bad request".
enum PilnkMode { PILNK_MODE_AM = 0 };
static int modeFromString(const std::string& m) {
    if (m == "AM") return PILNK_MODE_AM;
    return -1;
}
static bool modeKnownButUnsupported(const std::string& m) {
    return m=="NFM"||m=="FM"||m=="WFM"||m=="DSB"||m=="USB"||m=="CW"||m=="LSB"||m=="RAW";
}
static const char* modeToString(int m) {
    switch (m) { case PILNK_MODE_AM: return "AM"; default: return ""; }
}

// ======================= config =======================
// Explicit read/modify/write — no autosave-on-shutdown (that pattern gave us
// the sdrpp squelch-zombie). Saved atomically (tmp + rename) on every
// state-changing command, so `playing` (operator consent) survives restarts.
struct Config {
    std::string path;

    std::string serial   = "00000002";
    int         ppm      = -7;
    double      vfoHz    = 124300000.0;
    std::string mode     = "AM";
    double      bandwidthHz = 8000.0;
    int         gainIndex   = 21;
    bool        agc      = false;
    bool        squelchEnabled = false;
    double      squelchLevel   = -50.0;
    bool        playing  = false;      // consent: off by default, persisted
    int         port     = 5656;
    std::vector<std::string> allowedOrigins; // M3 enforcement lands in milestone 5
    std::string token;                       // "

    bool load(const std::string& p) {
        path = p;
        std::ifstream f(p);
        if (!f.good()) return false;
        json j;
        try { j = json::parse(f); } catch (...) {
            logE("config: %s is not valid JSON — refusing to start (fix or delete it)", p.c_str());
            exit(1);
        }
        serial      = j.value("serial", serial);
        ppm         = j.value("ppm", ppm);
        vfoHz       = j.value("vfoHz", vfoHz);
        mode        = j.value("mode", mode);
        bandwidthHz = j.value("bandwidthHz", bandwidthHz);
        gainIndex   = j.value("gainIndex", gainIndex);
        agc         = j.value("agc", agc);
        squelchEnabled = j.value("squelchEnabled", squelchEnabled);
        squelchLevel   = j.value("squelchLevel", squelchLevel);
        playing     = j.value("playing", playing);
        port        = j.value("port", port);
        token       = j.value("token", token);
        if (j.contains("allowedOrigins")) allowedOrigins = j["allowedOrigins"].get<std::vector<std::string>>();
        return true;
    }

    void save() {
        json j;
        j["serial"] = serial; j["ppm"] = ppm; j["vfoHz"] = vfoHz;
        j["mode"] = mode; j["bandwidthHz"] = bandwidthHz;
        j["gainIndex"] = gainIndex; j["agc"] = agc;
        j["squelchEnabled"] = squelchEnabled; j["squelchLevel"] = squelchLevel;
        j["playing"] = playing; j["port"] = port;
        j["allowedOrigins"] = allowedOrigins; j["token"] = token;
        std::string tmp = path + ".tmp";
        { std::ofstream f(tmp); f << j.dump(4) << "\n"; }
        if (rename(tmp.c_str(), path.c_str()) != 0) logE("config: rename to %s failed: %s", path.c_str(), strerror(errno));
    }
};

// ======================= driver sanity =======================
// Fleet lesson (2026-07-09, the V4-deaf hunt): SDR++ silently loaded the
// stock Debian librtlsdr instead of the rtl-sdr-blog fork and the V4 ran
// ~10 dB desensitized with bogus gain/ppm behavior. The daemon logs which
// librtlsdr the loader actually mapped so a wrong-driver node is visible
// in one journal line instead of a day of RF archaeology.
static void logLoadedRtlsdr() {
    std::ifstream maps("/proc/self/maps");
    std::string line, found;
    while (std::getline(maps, line)) {
        if (line.find("librtlsdr") != std::string::npos) {
            size_t sl = line.find('/');
            if (sl != std::string::npos) { found = line.substr(sl); break; }
        }
    }
    if (found.empty()) logW("driver: no librtlsdr mapped yet (static link?)");
    else logI("driver: loaded %s", found.c_str());
}

// ======================= RTL-SDR source =======================
// Direct librtlsdr ownership — replaces SDR++'s rtl_sdr_source module.
// Serial-pinned open (never grab the ADS-B stick), tenths-dB gain list
// exposed as the same dB steps the bridge published.
class RtlSource {
public:
    // Self-heal contract: fatal device conditions call onFatal, which the
    // daemon wires to "log + exit nonzero" so systemd owns recovery.
    std::function<void(const char*)> onFatal;
    std::function<void(const uint8_t*, size_t)> onIq; // raw u8 IQ from the async loop

    bool open(const std::string& serial, int ppm, uint32_t sampleRate) {
        int count = rtlsdr_get_device_count();
        if (count <= 0) { logE("rtl: no devices on USB"); return false; }
        int index = -1;
        for (int i = 0; i < count; i++) {
            char mfg[256] = {0}, prod[256] = {0}, ser[256] = {0};
            rtlsdr_get_device_usb_strings(i, mfg, prod, ser);
            logI("rtl: device %d: %s %s serial=%s", i, mfg, prod, ser);
            if (serial == ser) index = i;
        }
        if (index < 0) {
            logE("rtl: serial %s not found among %d device(s) — refusing to guess (ADS-B stick protection)", serial.c_str(), count);
            return false;
        }
        if (rtlsdr_open(&dev, index) != 0) { dev = nullptr; logE("rtl: open(index %d) failed — device busy?", index); return false; }
        if (rtlsdr_set_sample_rate(dev, sampleRate) != 0) { logE("rtl: set_sample_rate(%u) failed", sampleRate); return false; }
        this->rate = sampleRate;
        // librtlsdr rejects ppm 0 with -2 ("same value") on some versions; ignore that
        if (ppm != 0) rtlsdr_set_freq_correction(dev, ppm);
        // gain list: librtlsdr gives tenths of dB; publish dB (matches the
        // 29-step list the bridge exposed, so saved gainIndex carries over)
        int n = rtlsdr_get_tuner_gains(dev, nullptr);
        if (n > 0) {
            std::vector<int> tenths(n);
            rtlsdr_get_tuner_gains(dev, tenths.data());
            gainStepsDb.clear();
            for (int t : tenths) gainStepsDb.push_back(t / 10.0f);
        }
        rtlsdr_set_tuner_gain_mode(dev, 1); // manual
        logI("rtl: opened serial=%s rate=%u ppm=%d gains=%d", serial.c_str(), sampleRate, ppm, (int)gainStepsDb.size());
        return true;
    }

    void closeDev() {
        stopStream();
        if (dev) { rtlsdr_close(dev); dev = nullptr; }
    }

    bool setCenterHz(double hz) {
        if (!dev) return false;
        return rtlsdr_set_center_freq(dev, (uint32_t)llround(hz)) == 0;
    }
    void setGainIndex(int idx) {
        if (!dev || gainStepsDb.empty()) return;
        idx = std::clamp(idx, 0, (int)gainStepsDb.size() - 1);
        rtlsdr_set_tuner_gain_mode(dev, 1);
        rtlsdr_set_tuner_gain(dev, (int)llround(gainStepsDb[idx] * 10.0f));
    }
    void setAgc(bool on) {
        if (!dev) return;
        rtlsdr_set_tuner_gain_mode(dev, on ? 0 : 1);
        rtlsdr_set_agc_mode(dev, on ? 1 : 0);
    }

    bool startStream() {
        if (!dev || streaming) return false;
        rtlsdr_reset_buffer(dev);
        stopRequested = false;
        streaming = true;
        readThread = std::thread([this]() {
            // 16 buffers × 64 KB ≈ 218 ms of IQ at 2.4 MS/s — smooth on a Pi
            int r = rtlsdr_read_async(dev, &RtlSource::asyncTrampoline, this, 16, 65536);
            streaming = false;
            if (!stopRequested) {
                // async loop ended on its own = device lost (USB drop / driver
                // hang). This is THE invisible failure of the v1 stack.
                if (onFatal) onFatal("rtlsdr_read_async ended unexpectedly (device lost?)");
                (void)r;
            }
        });
        logI("rtl: streaming started");
        return true;
    }

    void stopStream() {
        if (!streaming && !readThread.joinable()) return;
        stopRequested = true;
        if (dev) rtlsdr_cancel_async(dev);
        if (readThread.joinable()) readThread.join();
        streaming = false;
        logI("rtl: streaming stopped");
    }

    bool isStreaming() const { return streaming; }
    const std::vector<float>& gains() const { return gainStepsDb; }
    uint32_t sampleRate() const { return rate; }

private:
    static void asyncTrampoline(unsigned char* buf, uint32_t len, void* ctx) {
        RtlSource* self = (RtlSource*)ctx;
        if (self->onIq && !self->stopRequested) self->onIq(buf, len);
    }
    rtlsdr_dev_t* dev = nullptr;
    std::thread readThread;
    std::atomic<bool> streaming{false};
    std::atomic<bool> stopRequested{false};
    std::vector<float> gainStepsDb;
    uint32_t rate = 2400000;
};

// ======================= DSP: decimating FIR =======================
// Windowed-sinc (Hamming) lowpass FIR over interleaved complex float,
// decimating by `factor` (factor 1 = plain filter). Block-based with carried
// history so phase is continuous across librtlsdr callbacks.
class FirDecimator {
public:
    void init(int factor, int nTaps, float cutoffNorm /* cycles/sample, <0.5 */) {
        this->factor = factor;
        taps.resize(nTaps);
        double sum = 0.0;
        for (int i = 0; i < nTaps; i++) {
            double x = (double)i - (nTaps - 1) / 2.0;
            double sinc = (x == 0.0) ? 2.0 * cutoffNorm
                                     : sin(2.0 * M_PI * cutoffNorm * x) / (M_PI * x);
            double hamm = 0.54 - 0.46 * cos(2.0 * M_PI * i / (nTaps - 1));
            taps[i] = (float)(sinc * hamm);
            sum += taps[i];
        }
        for (auto& t : taps) t = (float)(t / sum); // unity DC gain
        hist.assign((size_t)(nTaps - 1) * 2, 0.0f);
        phase = 0;
    }

    // in: nIn complex samples (2*nIn floats). out must hold nIn/factor+1 complex.
    // returns number of complex samples produced.
    size_t process(const float* in, size_t nIn, float* out) {
        size_t histC = hist.size() / 2;
        work.resize((histC + nIn) * 2);
        memcpy(work.data(), hist.data(), hist.size() * sizeof(float));
        memcpy(work.data() + hist.size(), in, nIn * 2 * sizeof(float));
        const int nT = (int)taps.size();
        size_t total = histC + nIn;
        size_t nOut = 0;
        // output at input positions p = (nT-1) + phase, stepping by factor
        size_t p = (size_t)(nT - 1) + phase;
        while (p < total) {
            float re = 0.0f, im = 0.0f;
            const float* w = work.data() + (p - (nT - 1)) * 2;
            for (int k = 0; k < nT; k++) {
                float h = taps[nT - 1 - k];
                re += h * w[2*k];
                im += h * w[2*k + 1];
            }
            out[2*nOut] = re; out[2*nOut + 1] = im;
            nOut++;
            p += factor;
        }
        phase = p - total; // carry sub-factor position into the next block
        // carry last nT-1 samples as history
        memcpy(hist.data(), work.data() + (total - histC) * 2, hist.size() * sizeof(float));
        return nOut;
    }

private:
    int factor = 1;
    size_t phase = 0;
    std::vector<float> taps, hist, work;
};

// ======================= live denoise (optional, v2.1.0) =======================
// AJ, 25 Sep 2026, after hearing DeepFilterNet3 on labelled clips: "almost crystal
// clear". The STT session measured it on EpsomPi — 1.36 ms mean per 10 ms frame,
// 14% of one Pi 5 core per listener, 0 of 5,987 frames late with STT running.
//
// ONE HARD RULE: LISTENERS ONLY. Denoised audio HALVES atc-stt's callsign accuracy
// (measured, 1,254 clips). So this never touches /sdr/audio itself — the stream the
// STT tap and flight capsules read stays exactly as it was. A browser opts in with
// /sdr/audio?denoise=1 and gets its OWN filtered copy, on its own worker thread.
//
// FAIL-SOFT, like everything else in this engine:
//   - the library is dlopen'd at runtime, never linked — a node without it builds
//     and runs exactly as before, and simply never offers denoise;
//   - before the engine loads it, a CHILD PROCESS loads it, builds a filter and
//     runs 20 frames. A wrong build for the CPU (the published binaries SIGBUS on
//     a Pi 5's 16 KB pages) kills the child, not the radio;
//   - a listener who asks when it is unavailable or full gets plain audio.
// Files (per node, per architecture — see atc-stt deploy/deepfilter-pi5.md):
//   /usr/local/lib/pilnk/libdf.so, /usr/local/share/pilnk/DeepFilterNet3_onnx.tar.gz
// (PILNK_DF_LIB / PILNK_DF_MODEL override, for testing.)
namespace dfn {
    typedef void*  (*create_t)(const char*, float, const char*);
    typedef size_t (*framelen_t)(void*);
    typedef float  (*process_t)(void*, float*, float*);
    typedef void   (*free_t)(void*);
    typedef char*  (*nextlog_t)(void*);
    typedef void   (*freelog_t)(char*);

    static const char* DEFAULT_LIB   = "/usr/local/lib/pilnk/libdf.so";
    static const char* DEFAULT_MODEL = "/usr/local/share/pilnk/DeepFilterNet3_onnx.tar.gz";
    static const float ATTEN_LIM_DB  = 100.0f;   // DFN's own default: no limit on suppression

    struct Api {
        std::string lib, model, why = "not probed";
        void* h = nullptr;
        create_t create = nullptr; framelen_t frameLen = nullptr;
        process_t process = nullptr; free_t destroy = nullptr;
        nextlog_t nextLog = nullptr; freelog_t freeLog = nullptr;   // optional
        bool ok = false;
    };
    static Api api;

    static bool fileExists(const std::string& p) {
        struct stat st;
        return !p.empty() && stat(p.c_str(), &st) == 0 && S_ISREG(st.st_mode);
    }

    static bool bindSyms(Api& a) {
        a.create   = (create_t)  dlsym(a.h, "df_create");
        a.frameLen = (framelen_t)dlsym(a.h, "df_get_frame_length");
        a.process  = (process_t) dlsym(a.h, "df_process_frame");
        a.destroy  = (free_t)    dlsym(a.h, "df_free");
        a.nextLog  = (nextlog_t) dlsym(a.h, "df_next_log_msg");
        a.freeLog  = (freelog_t) dlsym(a.h, "df_free_log_msg");
        return a.create && a.frameLen && a.process && a.destroy;
    }

    // The whole life of a filter, end to end. Run in the probe CHILD only.
    static int exercise(const std::string& lib, const std::string& model) {
        Api a;
        a.h = dlopen(lib.c_str(), RTLD_NOW | RTLD_LOCAL);
        if (!a.h) return 10;
        if (!bindSyms(a)) return 11;
        void* st = a.create(model.c_str(), ATTEN_LIM_DB, "error");
        if (!st) return 12;
        size_t fl = a.frameLen(st);
        if (fl == 0 || fl > 4096) return 13;
        std::vector<float> in(fl, 0.0f), out(fl, 0.0f);
        for (int i = 0; i < 20; i++) a.process(st, in.data(), out.data());
        a.destroy(st);
        return 0;
    }

    // Called from main() BEFORE any thread exists, so fork() is safe.
    static void probe() {
        const char* el = getenv("PILNK_DF_LIB");
        const char* em = getenv("PILNK_DF_MODEL");
        api.lib   = (el && *el) ? el : DEFAULT_LIB;
        api.model = (em && *em) ? em : DEFAULT_MODEL;
        if (!fileExists(api.lib))   { api.why = "no library at " + api.lib;  logI("denoise: off (%s)", api.why.c_str()); return; }
        if (!fileExists(api.model)) { api.why = "no model at " + api.model; logI("denoise: off (%s)", api.why.c_str()); return; }
        fflush(stdout);
        pid_t pid = fork();
        if (pid < 0) { api.why = "probe could not fork"; logW("denoise: off (%s)", api.why.c_str()); return; }
        if (pid == 0) { alarm(40); _exit(exercise(api.lib, api.model)); }   // backstop only: the parent gives up at 30 s
        int status = 0; pid_t r = 0;
        for (int i = 0; i < 300 && (r = waitpid(pid, &status, WNOHANG)) == 0; i++) usleep(100000);
        if (r == 0) { kill(pid, SIGKILL); waitpid(pid, &status, 0); api.why = "probe timed out after 30 s"; }
        else if (WIFSIGNALED(status)) api.why = "probe crashed (signal " + std::to_string(WTERMSIG(status)) + ") - wrong build for this machine?";
        else if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) api.why = "probe failed (code " + std::to_string(WEXITSTATUS(status)) + ")";
        else {
            api.h = dlopen(api.lib.c_str(), RTLD_NOW | RTLD_LOCAL);
            if (!api.h) { const char* e = dlerror(); api.why = std::string("dlopen: ") + (e ? e : "?"); }
            else if (!bindSyms(api)) api.why = "library lacks the df_* C API";
            else { api.ok = true; api.why = ""; }
        }
        if (api.ok) logI("denoise: available for listeners (%s)", api.lib.c_str());
        else        logW("denoise: off - %s (the radio itself is unaffected)", api.why.c_str());
    }
}

// ======================= daemon =======================
class PilnkRadioDaemon {
public:
    PilnkRadioDaemon(Config& cfg) : cfg(cfg) {}

    // returns false if the server could not bind (port taken => refuse to
    // start, prevents an accidental sdrpp+pilnk_bridge double-run on :5656)
    // or if the configured dongle is absent (exit => systemd/udev retries).
    bool start() {
        if (!startServer()) return false;
        logLoadedRtlsdr();
        // Device-independent setup, safe with or without a dongle. onIq/onFatal are
        // only ever invoked while streaming, which cannot happen without a device.
        rtl.onFatal = [](const char* why) {
            logE("FATAL: %s — exiting so systemd restarts us", why);
            exit(2);
        };
        rtl.onIq = [this](const uint8_t* d, size_t n) { iqHandler(d, n); };
        initFFT();
        initDsp();
        // #114 BIND-FIRST, ACQUIRE-LATER. A node with no radio dongle used to EXIT
        // here ("serial not found"), so systemd crash-looped it every 5s and the
        // state was indistinguishable from a broken engine (not_listening). Now: if
        // the dongle is present, wire it up; if not, keep serving /sdr/status with
        // an empty rfGainSteps list — which app.py reports as the honest 'no_device'
        // — and wait for the dongle in the background. "No dongle fitted" is a calm,
        // truthful state, not a fault and not a crash-loop.
        if (rtl.open(cfg.serial, cfg.ppm, SAMPLE_RATE)) {
            wireDevice();
        } else {
            logW("rtl: dongle serial %s not present — serving as no_device, waiting for it (retry every 5s)", cfg.serial.c_str());
            acquireThread = std::thread(&PilnkRadioDaemon::acquireLoop, this);
        }
        return true;
    }

    // Everything that needs an OPEN device. Called once the dongle is in hand — at
    // startup if it was already there, or from acquireLoop the moment it appears.
    void wireDevice() {
        applyTuning();
        rtl.setGainIndex(cfg.gainIndex);
        rtl.setAgc(cfg.agc);
        // controlThread refreshes /sdr/status (rfGainSteps etc.). It starts ONLY
        // here, after open() has finished populating the gain list, so it can never
        // race open() for gainStepsDb. Guarded so a future reopen can't double-start.
        if (!controlThread.joinable())
            controlThread = std::thread(&PilnkRadioDaemon::controlLoop, this);
        if (cfg.playing) {
            logI("consent was on (persisted) — resuming playback");
            rtl.startStream();
        }
    }

    // Background retry: open the configured dongle whenever it appears. This is the
    // whole point of #114 — the engine stays up as no_device and simply waits,
    // instead of exiting and being crash-looped by systemd.
    void acquireLoop() {
        while (!stopping) {
            for (int i = 0; i < 50 && !stopping; i++)   // ~5s, but wake fast on shutdown
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            if (stopping) break;
            if (rtl.open(cfg.serial, cfg.ppm, SAMPLE_RATE)) {
                logI("rtl: dongle serial %s appeared — wiring up", cfg.serial.c_str());
                wireDevice();
                return;
            }
        }
    }

    void stop() {
        stopping = true;
        if (acquireThread.joinable()) acquireThread.join();
        if (controlThread.joinable()) controlThread.join();
        rtl.closeDev();
        stopServer();
        freeFFT();
    }

    // ---- WS broadcast (called by FFT/audio producers; producers arrive in M2/M3) ----
    void broadcastFFT(const uint8_t* data, size_t len)   { fftFrameAccum.fetch_add(1, std::memory_order_relaxed); broadcast(fftSubs, data, len); }
    void broadcastAudio(const uint8_t* data, size_t len) {
        audioSampleAccum.fetch_add(len / sizeof(float), std::memory_order_relaxed);
        broadcast(audioSubs, data, len);                 // the raw stream: STT, capsules, plain listeners
        if (nDnSubs.load(std::memory_order_relaxed) > 0) // v2.1.0: a COPY for each denoised listener
            feedDenoise((const float*)data, len / sizeof(float));
    }

private:
    Config& cfg;
    std::atomic<bool> stopping{false};

    // -------- radio hardware + tuning layout --------
    // Hardware center sits below the VFO so the listening frequency is clear
    // of the RTL DC spike — same layout the SDR++ chain used (~950 kHz).
    static constexpr uint32_t SAMPLE_RATE = 2400000;
    static constexpr double   VFO_OFFSET  = 950000.0;
    RtlSource rtl;

    double hwCenterHz() const { return cfg.vfoHz - VFO_OFFSET; }

    void applyTuning() {
        double c = hwCenterHz();
        if (!rtl.setCenterHz(c)) logW("rtl: set_center_freq(%.0f) failed", c);
        curCenterHz.store(c);
        curSpanHz.store((double)SAMPLE_RATE);
    }

    // -------- FFT producer (ported from pilnk_bridge, adapted to u8 IQ) --------
    fftwf_complex* fftIn = nullptr;
    fftwf_complex* fftOut = nullptr;
    fftwf_plan fftPlan = nullptr;
    std::vector<float> window;
    int fillPos = 0;
    int skipRemaining = 0;
    std::atomic<double> curCenterHz{0};
    std::atomic<double> curSpanHz{0};
    std::atomic<int> nFftSubs{0};
    std::atomic<int> nAudioSubs{0};

    void initFFT() {
        window.resize(PILNK_FFT_SIZE);
        for (int i = 0; i < PILNK_FFT_SIZE; i++) {
            window[i] = (float)(0.42 - 0.5 * cos(2.0 * M_PI * i / (PILNK_FFT_SIZE - 1))
                                     + 0.08 * cos(4.0 * M_PI * i / (PILNK_FFT_SIZE - 1)));
        }
        fftIn  = fftwf_alloc_complex(PILNK_FFT_SIZE);
        fftOut = fftwf_alloc_complex(PILNK_FFT_SIZE);
        fftPlan = fftwf_plan_dft_1d(PILNK_FFT_SIZE, fftIn, fftOut, FFTW_FORWARD, FFTW_ESTIMATE);
        logI("fft: %d-point Blackman @ ~%d fps", PILNK_FFT_SIZE, (int)PILNK_FFT_RATE);
    }

    void freeFFT() {
        if (fftPlan) { fftwf_destroy_plan(fftPlan); fftPlan = nullptr; }
        if (fftIn)  { fftwf_free(fftIn);  fftIn = nullptr; }
        if (fftOut) { fftwf_free(fftOut); fftOut = nullptr; }
    }

    // called on the librtlsdr async thread with raw u8 IQ (I,Q interleaved)
    void iqHandler(const uint8_t* data, size_t len) {
        // demod always runs while streaming — audio flows whenever playing,
        // independent of clients (v1 semantics; audioSps is the watchdog key)
        demodBlock(data, len);
        if (nFftSubs.load() == 0) { fillPos = 0; skipRemaining = 0; return; }
        size_t nSamp = len / 2;
        for (size_t i = 0; i < nSamp; i++) {
            if (skipRemaining > 0) { skipRemaining--; continue; }
            // u8 -> centered float; 127.4 matches the rtl-sdr DC convention
            float re = ((float)data[2*i]   - 127.4f) * (1.0f / 128.0f);
            float im = ((float)data[2*i+1] - 127.4f) * (1.0f / 128.0f);
            fftIn[fillPos][0] = re * window[fillPos];
            fftIn[fillPos][1] = im * window[fillPos];
            fillPos++;
            if (fillPos >= PILNK_FFT_SIZE) {
                computeAndBroadcastFFT();
                fillPos = 0;
                double span = curSpanHz.load();
                int skip = (int)(span / PILNK_FFT_RATE) - PILNK_FFT_SIZE;
                skipRemaining = skip > 0 ? skip : 0;
            }
        }
    }

    void computeAndBroadcastFFT() {
        fftwf_execute(fftPlan);
        const int N = PILNK_FFT_SIZE;
        std::vector<uint8_t> frame(8 + 8 + 4 + (size_t)N * 4);
        double center = curCenterHz.load();
        double span = curSpanHz.load();
        std::memcpy(frame.data(), &center, 8);
        std::memcpy(frame.data() + 8, &span, 8);
        uint32_t nb = (uint32_t)N;
        std::memcpy(frame.data() + 16, &nb, 4);
        float* outdb = (float*)(frame.data() + 20);
        for (int p = 0; p < N; p++) {
            int src = (p + N / 2) % N; // fftshift -> ascending -span/2 .. +span/2
            float re = fftOut[src][0], im = fftOut[src][1];
            float mag = sqrtf(re * re + im * im) / (float)N;
            outdb[p] = 20.0f * log10f(mag + 1e-12f);
        }
        broadcastFFT(frame.data(), frame.size());
    }

    void refreshSubCounts() { // call under subsMtx
        nFftSubs.store((int)fftSubs.size());
        nAudioSubs.store((int)audioSubs.size());
    }

    // -------- AM demod chain (milestone 3) --------
    //   2.4 MS/s complex ── NCO (-950 kHz, exact 19/48 of fs => 48-entry
    //   phasor table, zero drift, no per-sample trig) ── FIR ÷10 → 240 kS/s
    //   ── FIR ÷5 → 48 kS/s ── channel LPF (bw/2) ── squelch ── AM envelope
    //   ── DC block ── AGC ── float32 mono 48 kHz → broadcastAudio
    // Runs entirely on the librtlsdr async thread (light: ~50 MMAC/s total).
    static constexpr int AUDIO_RATE = 48000;
    static constexpr int AUDIO_CHUNK = 512;   // samples per WS frame (matches v1 packer)
    std::vector<float> ncoTab;                // 48 phasors (re,im interleaved)
    size_t ncoIdx = 0;
    FirDecimator dec1, dec2, chanFir;
    std::mutex chanMtx;                       // guards chanFir rebuild vs DSP use
    std::atomic<bool> sqEnabled{false};
    std::atomic<float> sqLevel{-50.0f};
    float sqPowSmooth = 0.0f;                 // DSP thread only
    // Smoothed post-channel-filter power, published so the gain helper (and a
    // human tuning by hand) can see what the radio is actually hearing.
    // chanPowValid stays false until the DSP has run a block: a dead-silent
    // channel and "nothing has been measured yet" must not collapse into the
    // same number, so statusJson emits null rather than a floor value.
    std::atomic<bool>  chanPowValid{false};
    std::atomic<float> chanPowDb{0.0f};
    float dcState = 0.0f, dcPrev = 0.0f;      // DC blocker (one-pole HPF)
    float agcGain = 20.0f;                    // DSP thread only
    std::vector<float> audioAcc;              // pending output samples
    std::vector<float> dspBufA, dspBufB, dspBufC; // reused work buffers

    void initDsp() {
        // NCO: shift +950 kHz (VFO sits above hw center) down to DC.
        // step = -2π·(950000/2400000) = -2π·19/48 → exactly periodic in 48.
        ncoTab.resize(48 * 2);
        for (int i = 0; i < 48; i++) {
            double ph = -2.0 * M_PI * 19.0 * i / 48.0;
            ncoTab[2*i]   = (float)cos(ph);
            ncoTab[2*i+1] = (float)sin(ph);
        }
        ncoIdx = 0;
        dec1.init(10, 64, 100000.0f / 2400000.0f);  // 2.4M -> 240k, cutoff 100 kHz
        dec2.init(5,  64, 20000.0f  / 240000.0f);   // 240k -> 48k,  cutoff 20 kHz
        rebuildChannelFilter(cfg.bandwidthHz);
        sqEnabled.store(cfg.squelchEnabled);
        sqLevel.store((float)cfg.squelchLevel);
    }

    void rebuildChannelFilter(double bwHz) {
        double cutoff = std::clamp(bwHz / 2.0, 1500.0, 20000.0);
        std::lock_guard<std::mutex> lk(chanMtx);
        chanFir.init(1, 101, (float)(cutoff / AUDIO_RATE));
        logI("dsp: channel LPF rebuilt (bw %.0f Hz, cutoff %.0f Hz)", bwHz, cutoff);
    }

    std::vector<float>* testCapture = nullptr; // --selftest audio tap

    // demod tap — called from iqHandler with the raw u8 block
    void demodBlock(const uint8_t* data, size_t len) {
        size_t nSamp = len / 2;
        dspBufA.resize(nSamp * 2);
        // u8 -> float with NCO rotation fused in one pass
        for (size_t i = 0; i < nSamp; i++) {
            float re = ((float)data[2*i]   - 127.4f) * (1.0f / 128.0f);
            float im = ((float)data[2*i+1] - 127.4f) * (1.0f / 128.0f);
            float cr = ncoTab[2*ncoIdx], ci = ncoTab[2*ncoIdx + 1];
            dspBufA[2*i]   = re * cr - im * ci;
            dspBufA[2*i+1] = re * ci + im * cr;
            ncoIdx = (ncoIdx + 1) % 48;
        }
        dspBufB.resize(nSamp / 10 * 2 + 4);
        size_t n1 = dec1.process(dspBufA.data(), nSamp, dspBufB.data());
        dspBufC.resize(n1 / 5 * 2 + 4);
        size_t n2 = dec2.process(dspBufB.data(), n1, dspBufC.data());
        dspBufA.resize(n2 * 2 + 4);
        size_t n3;
        { std::lock_guard<std::mutex> lk(chanMtx);
          n3 = chanFir.process(dspBufC.data(), n2, dspBufA.data()); }

        // squelch: smoothed channel power in dBFS (post channel filter)
        float pow = 0.0f;
        for (size_t i = 0; i < n3; i++)
            pow += dspBufA[2*i]*dspBufA[2*i] + dspBufA[2*i+1]*dspBufA[2*i+1];
        if (n3) pow /= (float)n3;
        sqPowSmooth += 0.2f * (pow - sqPowSmooth);
        // dB is computed unconditionally now. It used to be derived only when
        // squelch happened to be enabled and thrown away otherwise — the one
        // number a gain helper needs, calculated every block and discarded.
        const float chanDb = 10.0f * log10f(sqPowSmooth + 1e-12f);
        if (n3) {   // an empty block is not a measurement of silence
            chanPowDb.store(chanDb, std::memory_order_relaxed);
            chanPowValid.store(true, std::memory_order_relaxed);
        }
        bool muted = false;
        if (sqEnabled.load(std::memory_order_relaxed))
            muted = chanDb < sqLevel.load(std::memory_order_relaxed);

        // AM envelope -> DC block -> AGC
        for (size_t i = 0; i < n3; i++) {
            float mag = sqrtf(dspBufA[2*i]*dspBufA[2*i] + dspBufA[2*i+1]*dspBufA[2*i+1]);
            // one-pole DC blocker (~8 Hz at 48k): strips the AM carrier level
            float y = mag - dcPrev + 0.999f * dcState;
            dcPrev = mag; dcState = y;
            float s = y * agcGain;
            // AGC: fast attack on overshoot, slow decay toward target
            float a = fabsf(s);
            if (a > 0.85f)      agcGain *= 0.98f;
            else if (a < 0.15f) agcGain *= 1.0002f;
            agcGain = std::clamp(agcGain, 0.5f, 200.0f);
            audioAcc.push_back(muted ? 0.0f : std::clamp(s, -1.0f, 1.0f));
        }

        // emit fixed 512-sample chunks (same cadence the v1 packer produced)
        size_t off = 0;
        while (audioAcc.size() - off >= AUDIO_CHUNK) {
            if (testCapture) testCapture->insert(testCapture->end(),
                                                 audioAcc.begin() + off, audioAcc.begin() + off + AUDIO_CHUNK);
            else broadcastAudio((const uint8_t*)(audioAcc.data() + off), AUDIO_CHUNK * sizeof(float));
            off += AUDIO_CHUNK;
        }
        if (off) audioAcc.erase(audioAcc.begin(), audioAcc.begin() + off);
    }

public:
    // ---- DSP self-test (no hardware): synthesize a textbook AM signal at the
    // VFO offset and demodulate it through the real chain. Fleet installers
    // run `pilnkradio --selftest` to validate DSP on any node, no antenna.
    // Returns 0 on PASS.
    int selfTest() {
        initDsp();
        std::vector<float> out;
        testCapture = &out;
        // 2 s of u8 IQ at 2.4 MS/s: carrier at +950 kHz (the VFO slot),
        // 30% AM by a 1 kHz tone, amplitude 0.5 FS + light noise floor
        const size_t N = 2 * SAMPLE_RATE;
        std::vector<uint8_t> iq(N * 2);
        double ph = 0.0, phStep = 2.0 * M_PI * (VFO_OFFSET / (double)SAMPLE_RATE);
        uint32_t rng = 0x12345678;
        for (size_t i = 0; i < N; i++) {
            double t = (double)i / SAMPLE_RATE;
            double env = 0.5 * (1.0 + 0.3 * sin(2.0 * M_PI * 1000.0 * t));
            rng = rng * 1664525u + 1013904223u;
            double nz = ((int)(rng >> 16 & 0xFF) - 128) / 128.0 * 0.01;
            iq[2*i]   = (uint8_t)std::clamp(127.4 + 128.0 * (env * cos(ph) + nz), 0.0, 255.0);
            iq[2*i+1] = (uint8_t)std::clamp(127.4 + 128.0 * (env * sin(ph) + nz), 0.0, 255.0);
            ph += phStep;
        }
        for (size_t off = 0; off < N * 2; off += 65536)
            demodBlock(iq.data() + off, std::min((size_t)65536, N * 2 - off));
        testCapture = nullptr;
        if (out.size() < AUDIO_RATE) { logE("selftest: only %zu audio samples out", out.size()); return 1; }
        // analyze the last second (AGC/DC settled): Goertzel at 1 kHz vs total
        const float* x = out.data() + (out.size() - AUDIO_RATE);
        const int n = AUDIO_RATE;
        double total = 0.0;
        for (int i = 0; i < n; i++) total += (double)x[i] * x[i];
        double w = 2.0 * M_PI * 1000.0 / AUDIO_RATE, c = 2.0 * cos(w);
        double s0 = 0, s1 = 0, s2 = 0;
        for (int i = 0; i < n; i++) { s0 = x[i] + c * s1 - s2; s2 = s1; s1 = s0; }
        // |X(1kHz)|² = s1²+s2²−c·s1·s2; a real tone of amplitude A gives
        // |X| = A·n/2, so per-sample tone power A²/2 = 2·|X|²/n²
        double tonePow = (s1*s1 + s2*s2 - c*s1*s2) * 2.0 / ((double)n * n);
        double toneFrac = tonePow / (total / n + 1e-12);
        double rate = out.size() / 2.0;
        logI("selftest: %zu samples (%.0f sps), 1 kHz tone fraction %.3f", out.size(), rate, toneFrac);
        bool pass = toneFrac > 0.90 && rate > 47000 && rate < 49000;
        logLine(pass ? "INF" : "ERR", "selftest: %s", pass ? "PASS — demod chain produces a clean 1 kHz tone" : "FAIL");
        return pass ? 0 : 1;
    }

private:

    // -------- control command queue (drained on the control thread) --------
    // Same queue shape as the bridge; the drain no longer needs a GUI thread —
    // that constraint (and the whole Xvfb requirement) dies with SDR++.
    struct Command {
        enum Type { FREQ, MODE, BANDWIDTH, PLAYING, SQUELCH_MODE, SQUELCH_LEVEL, GAIN_INDEX, AGC } type;
        double dval = 0; int ival = 0; bool bval = false;
    };
    std::mutex cmdMtx;
    std::deque<Command> cmdQueue;

    struct StatusSnapshot {
        double centerHz = 0, vfoHz = 0, bandwidthHz = 0;
        int mode = PILNK_MODE_AM;
        bool playing = false;
        int audioSampleRate = 48000;
        bool squelchEnabled = false;
        float squelchLevel = -50.0f;
        std::vector<float> rfGainSteps;  // empty until a device is open (M2) —
                                         // deliberately keeps the "no device"
                                         // tell the tab/watchdog key on
        int rfGainIndex = 0;
        bool rfAgc = false;
        int fftFps = 0;
        int audioSps = 0;
        bool  channelPowerValid = false; // false => statusJson emits null
        float channelPowerDb = 0.0f;     // post-channel-filter power, dBFS
    };
    std::mutex statusMtx;
    StatusSnapshot status;

    void enqueue(const Command& c) {
        std::lock_guard<std::mutex> lk(cmdMtx);
        cmdQueue.push_back(c);
    }

    // -------- control thread: drain commands, refresh status, sample flow --------
    std::thread controlThread;
    std::thread acquireThread;   // #114: background wait-for-dongle when none is present at startup

    void controlLoop() {
        while (!stopping) {
            processCommands();
            refreshStatus();
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }

    void processCommands() {
        std::deque<Command> local;
        { std::lock_guard<std::mutex> lk(cmdMtx); local.swap(cmdQueue); }
        if (local.empty()) return;
        for (auto& c : local) {
            switch (c.type) {
                case Command::PLAYING:
                    cfg.playing = c.bval;
                    if (c.bval) rtl.startStream(); else rtl.stopStream();
                    // demodBlock only runs while the stream does, so a stopped
                    // radio would otherwise keep publishing its last reading
                    // as though it were current. Go back to "not measured".
                    if (!c.bval) chanPowValid.store(false, std::memory_order_relaxed);
                    logI("control: playing -> %s", c.bval ? "true" : "false");
                    break;
                case Command::FREQ:
                    cfg.vfoHz = c.dval;
                    applyTuning();
                    // milestone 3: retune the NCO with the hardware
                    logI("control: vfo -> %.0f Hz", c.dval);
                    break;
                case Command::MODE:
                    cfg.mode = modeToString(c.ival);
                    break;
                case Command::BANDWIDTH:
                    cfg.bandwidthHz = c.dval;
                    rebuildChannelFilter(c.dval);
                    break;
                case Command::SQUELCH_MODE:
                    cfg.squelchEnabled = c.bval;
                    sqEnabled.store(c.bval);
                    break;
                case Command::SQUELCH_LEVEL:
                    cfg.squelchLevel = std::clamp(c.dval, -100.0, 0.0);
                    sqLevel.store((float)cfg.squelchLevel);
                    break;
                case Command::GAIN_INDEX:
                    cfg.gainIndex = std::clamp(c.ival, 0, std::max(0, (int)rtl.gains().size() - 1));
                    rtl.setGainIndex(cfg.gainIndex);
                    break;
                case Command::AGC:
                    cfg.agc = c.bval;
                    rtl.setAgc(c.bval);
                    break;
            }
        }
        cfg.save(); // persist every applied change (incl. consent)
    }

    void refreshStatus() {
        StatusSnapshot s;
        s.vfoHz = cfg.vfoHz;
        s.centerHz = hwCenterHz();
        s.bandwidthHz = cfg.bandwidthHz;
        s.mode = modeFromString(cfg.mode);
        s.playing = cfg.playing;
        s.squelchEnabled = cfg.squelchEnabled;
        s.squelchLevel = (float)cfg.squelchLevel;
        s.rfGainIndex = cfg.gainIndex;
        s.rfAgc = cfg.agc;
        s.rfGainSteps = rtl.gains(); // empty list = no device: the tell the
                                     // tab/watchdog key on stays truthful
        s.channelPowerValid = chanPowValid.load(std::memory_order_relaxed);
        s.channelPowerDb    = chanPowDb.load(std::memory_order_relaxed);
        sampleFlow(s);
        { std::lock_guard<std::mutex> lk(statusMtx); status = s; }
    }

    std::string statusJson() {
        StatusSnapshot s;
        { std::lock_guard<std::mutex> lk(statusMtx); s = status; }
        json j;
        j["centerHz"] = s.centerHz;
        j["vfoHz"] = s.vfoHz;
        j["mode"] = s.mode >= 0 ? modeToString(s.mode) : "";
        j["bandwidthHz"] = s.bandwidthHz;
        j["playing"] = s.playing;
        j["audioSampleRate"] = s.audioSampleRate;
        j["squelchEnabled"] = s.squelchEnabled;
        j["squelchLevel"] = s.squelchLevel;
        j["rfGainSteps"] = s.rfGainSteps;
        j["rfGainIndex"] = s.rfGainIndex;
        j["rfAgc"] = s.rfAgc;
        j["fftFps"] = s.fftFps;
        j["audioSps"] = s.audioSps;
        // null, never a floor value: "quiet" and "not measured" stay distinct
        if (s.channelPowerValid) j["channelPowerDb"] = s.channelPowerDb;
        else                     j["channelPowerDb"] = nullptr;
        // WHO is answering on this port, and which build. A node can have the
        // current binary installed while a DIFFERENT program holds :5656 —
        // sdrpp did exactly that on LINKLABS for 28 days, serving /sdr/status
        // the whole time while pilnkradio failed to bind 393,391 times. From
        // outside, the two were indistinguishable. These two keys make "is this
        // node running the current engine?" a single curl, fleet-wide.
        j["engine"]  = "pilnkradio";
        j["version"] = PILNKRADIO_VERSION;
        // v2.1.0: whether a listener can ask for /sdr/audio?denoise=1, and if not, why.
        json dn = { {"available", dfn::api.ok}, {"clients", nDnSubs.load()}, {"max", DN_MAX} };
        if (!dfn::api.ok) dn["why"] = dfn::api.why;
        j["denoise"] = dn;
        return j.dump();
    }

    // -------- zero-sample-flow counters --------
    // Same design as the bridge: producers bump atomics, the control thread
    // samples them into per-second rates. In the daemon these mostly serve the
    // tab's stall banner + external watchdog during transition — the primary
    // self-heal is now "device lost => daemon exits => systemd restarts".
    std::atomic<uint64_t> fftFrameAccum{0};
    std::atomic<uint64_t> audioSampleAccum{0};
    std::atomic<int> fftFpsVal{0};
    std::atomic<int> audioSpsVal{0};
    std::chrono::steady_clock::time_point flowSampleTime = std::chrono::steady_clock::now();
    double zeroFlowSecs = 0.0;   // control thread only
    bool flowWarned = false;     // control thread only

    void sampleFlow(StatusSnapshot& s) { // called from refreshStatus (control thread)
        auto now = std::chrono::steady_clock::now();
        double dt = std::chrono::duration<double>(now - flowSampleTime).count();
        if (dt >= 1.0) {
            uint64_t ff = fftFrameAccum.exchange(0, std::memory_order_relaxed);
            uint64_t as = audioSampleAccum.exchange(0, std::memory_order_relaxed);
            fftFpsVal.store((int)llround((double)ff / dt), std::memory_order_relaxed);
            audioSpsVal.store((int)llround((double)as / dt), std::memory_order_relaxed);
            flowSampleTime = now;
            if (s.playing && ff == 0 && as == 0) {
                zeroFlowSecs += dt;
                if (zeroFlowSecs > 5.0 && !flowWarned) {
                    logW("playing but zero FFT/audio flow for %ds - source stalled (USB dongle dropped?)", (int)zeroFlowSecs);
                    flowWarned = true;
                }
            } else {
                if (flowWarned && (ff || as)) {
                    logI("sample flow recovered (%d fft fps, %d audio sps)",
                         fftFpsVal.load(std::memory_order_relaxed), audioSpsVal.load(std::memory_order_relaxed));
                }
                zeroFlowSecs = 0.0;
                flowWarned = false; // rearm
            }
        }
        s.fftFps = fftFpsVal.load(std::memory_order_relaxed);
        s.audioSps = audioSpsVal.load(std::memory_order_relaxed);
    }

    // ============================ server ============================
    // Ported verbatim from pilnk_bridge @ 5aefe59, audit fixes intact.
    std::atomic<bool> running{false};
    // M1 (audit 2026-07-09): bound concurrent connections so a flood can't
    // exhaust the thread-per-conn model. 64 is generous for a LAN node
    // (dashboard uses 2 WS + short-lived status polls).
    std::atomic<int> activeConns{0};
    static const int MAX_CONNS = 64;
    int listenFd = -1;
    std::thread acceptThread;

    std::mutex subsMtx;
    std::vector<int> fftSubs;
    std::vector<int> audioSubs;

    // -------- denoised listeners (v2.1.0, see dfn:: above) --------
    // The DSP thread only COPIES each 512-sample chunk into a per-listener queue;
    // the filtering (about 1.4 ms per 10 ms frame on a Pi 5) runs on that listener's
    // own worker thread and is written to its socket there. A slow filter or a slow
    // browser can therefore never stall the raw stream that STT and capsules read.
    struct DnClient {
        int fd = -1;
        std::mutex wmx;                         // one writer at a time: filtered frames vs pongs
        std::mutex qmx;
        std::condition_variable qcv;
        std::deque<std::vector<float>> q;
        std::atomic<bool> dead{false};
        std::thread worker;
        // handleWS joins the worker before letting go; this only matters at process
        // exit, where a still-joinable std::thread would otherwise call std::terminate.
        ~DnClient() { if (worker.joinable()) worker.detach(); }
    };
    static constexpr int    DN_MAX  = 2;        // ~14% of a Pi 5 core each; STT runs alongside
    static constexpr size_t DN_QMAX = 48;       // ~0.5 s: a listener further behind loses the oldest
    std::mutex dnMtx;
    std::vector<std::shared_ptr<DnClient>> dnSubs;
    std::atomic<int> nDnSubs{0};

    void feedDenoise(const float* s, size_t n) {       // DSP thread
        std::lock_guard<std::mutex> lk(dnMtx);
        for (auto& c : dnSubs) {
            if (c->dead.load()) continue;
            { std::lock_guard<std::mutex> ql(c->qmx);
              if (c->q.size() >= DN_QMAX) c->q.pop_front();
              c->q.emplace_back(s, s + n); }
            c->qcv.notify_one();
        }
    }

    static size_t wsHeader(uint8_t hdr[10], size_t len) {   // server->client binary frame, unmasked
        hdr[0] = 0x82;
        if (len < 126) { hdr[1] = (uint8_t)len; return 2; }
        if (len <= 0xFFFF) { hdr[1] = 126; hdr[2] = (len >> 8) & 0xFF; hdr[3] = len & 0xFF; return 4; }
        hdr[1] = 127; for (int i = 0; i < 8; i++) hdr[2+i] = (len >> (8 * (7 - i))) & 0xFF; return 10;
    }

    void dnWorker(std::shared_ptr<DnClient> c) {
        void* st = dfn::api.create(dfn::api.model.c_str(), dfn::ATTEN_LIM_DB, "error");
        if (!st) {
            logW("denoise: could not build a filter for a listener - closing it");
            c->dead = true; shutdown(c->fd, SHUT_RDWR); return;
        }
        const size_t fl = dfn::api.frameLen(st);           // 480 = 10 ms @ 48 kHz for DFN3
        std::vector<float> in(fl), out(fl), pend, outBuf;
        auto lastDrain = std::chrono::steady_clock::now();
        while (!c->dead.load() && running) {
            std::vector<float> chunk;
            {
                std::unique_lock<std::mutex> lk(c->qmx);
                c->qcv.wait_for(lk, std::chrono::milliseconds(250),
                                [&]{ return !c->q.empty() || c->dead.load(); });
                if (c->q.empty()) continue;
                chunk.swap(c->q.front()); c->q.pop_front();
            }
            // The engine packs 512-sample chunks, DFN wants 480-sample frames: re-frame.
            pend.insert(pend.end(), chunk.begin(), chunk.end());
            size_t off = 0;
            while (pend.size() - off >= fl) {
                std::memcpy(in.data(), pend.data() + off, fl * sizeof(float));
                dfn::api.process(st, in.data(), out.data());
                outBuf.insert(outBuf.end(), out.begin(), out.end());
                off += fl;
            }
            if (off) pend.erase(pend.begin(), pend.begin() + off);
            if (!outBuf.empty()) {
                uint8_t hdr[10]; size_t len = outBuf.size() * sizeof(float);
                size_t hl = wsHeader(hdr, len);
                std::lock_guard<std::mutex> wl(c->wmx);
                if (!sendAll(c->fd, hdr, hl) || !sendAll(c->fd, outBuf.data(), len)) { c->dead = true; break; }
                outBuf.clear();
            }
            // "error"-level log lines queue inside the library until read: drain them.
            auto now = std::chrono::steady_clock::now();
            if (dfn::api.nextLog && dfn::api.freeLog && now - lastDrain > std::chrono::seconds(10)) {
                for (char* m; (m = dfn::api.nextLog(st)) != nullptr; ) { logW("denoise: %s", m); dfn::api.freeLog(m); }
                lastDrain = now;
            }
        }
        dfn::api.destroy(st);
        shutdown(c->fd, SHUT_RDWR);   // wakes handleWS's recv so it tidies up and closes the fd
    }

    bool startServer() {
        listenFd = socket(AF_INET, SOCK_STREAM, 0);
        if (listenFd < 0) { logE("socket() failed"); return false; }
        int one = 1;
        setsockopt(listenFd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = INADDR_ANY;
        addr.sin_port = htons(cfg.port);
        if (bind(listenFd, (sockaddr*)&addr, sizeof(addr)) < 0) {
            logE("bind(:%d) failed: %s — is sdrpp/pilnk_bridge still running?", cfg.port, strerror(errno));
            close(listenFd); listenFd = -1; return false;
        }
        if (listen(listenFd, 8) < 0) {
            logE("listen() failed");
            close(listenFd); listenFd = -1; return false;
        }
        running = true;
        acceptThread = std::thread(&PilnkRadioDaemon::acceptLoop, this);
        logI("HTTP/WS server listening on 0.0.0.0:%d", cfg.port);
        return true;
    }

    void stopServer() {
        running = false;
        if (listenFd >= 0) { shutdown(listenFd, SHUT_RDWR); close(listenFd); listenFd = -1; }
        if (acceptThread.joinable()) acceptThread.join();
        std::lock_guard<std::mutex> lk(subsMtx);
        for (int fd : fftSubs) close(fd);
        for (int fd : audioSubs) close(fd);
        fftSubs.clear(); audioSubs.clear();
        // denoised listeners close themselves: flag them, wake them, unblock their sockets
        std::lock_guard<std::mutex> dl(dnMtx);
        for (auto& c : dnSubs) { c->dead = true; c->qcv.notify_all(); shutdown(c->fd, SHUT_RDWR); }
    }

    void acceptLoop() {
        while (running) {
            int fd = accept(listenFd, NULL, NULL);
            if (fd < 0) { if (running) continue; else break; }
            if (activeConns.load(std::memory_order_relaxed) >= MAX_CONNS) {
                close(fd);   // M1: too many connections — shed load
                continue;
            }
            activeConns.fetch_add(1, std::memory_order_relaxed);
            std::thread(&PilnkRadioDaemon::handleConn, this, fd).detach();
        }
    }

    static bool sendAll(int fd, const void* buf, size_t len) {
        const uint8_t* p = (const uint8_t*)buf; size_t sent = 0;
        while (sent < len) {
            ssize_t n = send(fd, p + sent, len - sent, MSG_NOSIGNAL);
            if (n <= 0) return false;
            sent += n;
        }
        return true;
    }

    void httpResponse(int fd, const char* status, const char* ctype, const std::string& body) {
        char hdr[512];
        int n = snprintf(hdr, sizeof(hdr),
            "HTTP/1.1 %s\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %zu\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            "Access-Control-Allow-Headers: Content-Type\r\n"
            "Connection: close\r\n\r\n",
            status, ctype, body.size());
        sendAll(fd, hdr, n);
        if (!body.empty()) sendAll(fd, body.data(), body.size());
    }

    // header lookup (case-insensitive) in a raw request string
    static std::string header(const std::string& req, const std::string& key) {
        std::string lk = key; for (auto& ch : lk) ch = tolower(ch);
        size_t pos = 0;
        while (true) {
            size_t eol = req.find("\r\n", pos);
            if (eol == std::string::npos) break;
            std::string line = req.substr(pos, eol - pos);
            size_t colon = line.find(':');
            if (colon != std::string::npos) {
                std::string hk = line.substr(0, colon);
                for (auto& ch : hk) ch = tolower(ch);
                if (hk == lk) {
                    std::string v = line.substr(colon + 1);
                    size_t s = v.find_first_not_of(" \t");
                    return s == std::string::npos ? "" : v.substr(s);
                }
            }
            pos = eol + 2;
        }
        return "";
    }

    // -------- Origin / token gate (audit M3, 2026-07-09) --------
    // A no-cors POST from ANY website the operator visits (or DNS rebinding)
    // could otherwise flip playing:true. Rules:
    //  - no Origin header (curl, watchdog, scripts) => allowed
    //  - localhost origins => allowed
    //  - otherwise the origin must be in config allowedOrigins (the fleet
    //    installer sets this to the node's dashboard origin)
    // Token (optional, config "token"): required on POST via X-PiLNK-Token.
    bool originAllowed(const std::string& req) {
        std::string origin = header(req, "Origin");
        if (origin.empty()) return true;
        if (origin.rfind("http://localhost", 0) == 0 || origin.rfind("http://127.0.0.1", 0) == 0 ||
            origin.rfind("https://localhost", 0) == 0 || origin.rfind("https://127.0.0.1", 0) == 0) return true;
        for (auto& a : cfg.allowedOrigins) if (origin == a) return true;
        logW("origin rejected: %s", origin.c_str());
        return false;
    }

    void handleConn(int fd) {
        // M1: RAII — decrement the active-connection count on EVERY exit path
        // (early returns, WS lifetime, errors). acceptLoop already incremented.
        struct ConnGuard { std::atomic<int>& c; ~ConnGuard() { c.fetch_sub(1, std::memory_order_relaxed); } } _cg{activeConns};
        int one = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        // M1: recv timeout bounds slowloris (client that connects and dribbles
        // or never completes its request headers). 15s is ample for a real
        // request; the WS keep-alive loop below tolerates this timeout so idle
        // subscribers are NOT dropped.
        struct timeval rcvto{15, 0};
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &rcvto, sizeof(rcvto));
        // read request headers
        std::string req;
        char buf[2048];
        while (req.find("\r\n\r\n") == std::string::npos) {
            ssize_t n = recv(fd, buf, sizeof(buf), 0);
            if (n <= 0) { close(fd); return; }
            req.append(buf, n);
            if (req.size() > 65536) { close(fd); return; }
        }
        // parse request line
        size_t sp1 = req.find(' ');
        size_t sp2 = req.find(' ', sp1 + 1);
        if (sp1 == std::string::npos || sp2 == std::string::npos) { close(fd); return; }
        std::string method = req.substr(0, sp1);
        std::string path = req.substr(sp1 + 1, sp2 - sp1 - 1);

        // Origin gate (audit M3) — applies to WS upgrades and POSTs alike
        if (!originAllowed(req)) {
            httpResponse(fd, "403 Forbidden", "application/json", "{\"ok\":false,\"error\":\"origin not allowed\"}");
            close(fd); return;
        }

        // WebSocket upgrade?
        std::string upg = header(req, "Upgrade");
        for (auto& ch : upg) ch = tolower(ch);
        if (upg == "websocket") { handleWS(fd, req, path); return; }

        if (method == "OPTIONS") { httpResponse(fd, "204 No Content", "text/plain", ""); close(fd); return; }

        // token gate (audit M3, optional) — control writes only
        if (method == "POST" && !cfg.token.empty() && header(req, "X-PiLNK-Token") != cfg.token) {
            httpResponse(fd, "401 Unauthorized", "application/json", "{\"ok\":false,\"error\":\"bad token\"}");
            close(fd); return;
        }

        // read body if present
        std::string body;
        size_t hdrEnd = req.find("\r\n\r\n") + 4;
        body = req.substr(hdrEnd);
        int clen = 0;
        try { clen = std::stoi(header(req, "Content-Length")); } catch (...) { clen = 0; }
        // M2 (audit 2026-07-09): cap the body. Control JSON is tiny; a request
        // advertising a huge Content-Length would otherwise grow `body`
        // unbounded (memory-exhaustion DoS). Reject anything over 64 KB.
        if (clen < 0) clen = 0;
        if (clen > 65536) {
            httpResponse(fd, "413 Payload Too Large", "application/json", "{\"ok\":false,\"error\":\"body too large\"}");
            close(fd); return;
        }
        while ((int)body.size() < clen) {
            ssize_t n = recv(fd, buf, sizeof(buf), 0);
            if (n <= 0) break;
            body.append(buf, n);
        }

        routeHttp(fd, method, path, body);
        close(fd);
    }

    void routeHttp(int fd, const std::string& method, const std::string& path, const std::string& body) {
        if (method == "GET" && path == "/sdr/status") {
            httpResponse(fd, "200 OK", "application/json", statusJson());
            return;
        }
        if (method == "POST") {
            json b;
            try { b = json::parse(body); } catch (...) {
                httpResponse(fd, "400 Bad Request", "application/json", "{\"ok\":false,\"error\":\"bad json\"}");
                return;
            }
            Command c;
            bool ok = true;
            std::string err = "bad request";
            if (path == "/sdr/frequency" && b.contains("hz")) {
                c.type = Command::FREQ; c.dval = b["hz"].get<double>(); enqueue(c);
            } else if (path == "/sdr/mode" && b.contains("mode")) {
                std::string ms = b["mode"].get<std::string>();
                int m = modeFromString(ms);
                if (m < 0) { ok = false; if (modeKnownButUnsupported(ms)) err = "mode not supported (v2 is AM-only)"; }
                else { c.type = Command::MODE; c.ival = m; enqueue(c); }
            } else if (path == "/sdr/bandwidth" && b.contains("hz")) {
                c.type = Command::BANDWIDTH; c.dval = b["hz"].get<double>(); enqueue(c);
            } else if (path == "/sdr/playing" && b.contains("on")) {
                c.type = Command::PLAYING; c.bval = b["on"].get<bool>(); enqueue(c);
            } else if (path == "/sdr/squelch" && (b.contains("enabled") || b.contains("level"))) {
                if (b.contains("enabled")) {
                    Command sm; sm.type = Command::SQUELCH_MODE; sm.bval = b["enabled"].get<bool>(); enqueue(sm);
                }
                if (b.contains("level")) {
                    Command sl; sl.type = Command::SQUELCH_LEVEL; sl.dval = b["level"].get<double>(); enqueue(sl);
                }
            } else if (path == "/sdr/gain" && b.contains("index")) {
                c.type = Command::GAIN_INDEX; c.ival = b["index"].get<int>(); enqueue(c);
            } else if (path == "/sdr/agc" && b.contains("on")) {
                c.type = Command::AGC; c.bval = b["on"].get<bool>(); enqueue(c);
            } else {
                ok = false;
            }
            httpResponse(fd, ok ? "200 OK" : "400 Bad Request", "application/json",
                         ok ? "{\"ok\":true}" : std::string("{\"ok\":false,\"error\":\"") + err + "\"}");
            return;
        }
        httpResponse(fd, "404 Not Found", "application/json", "{\"ok\":false,\"error\":\"not found\"}");
    }

    // -------- WebSocket --------
    void handleWS(int fd, const std::string& req, const std::string& path) {
        std::string key = header(req, "Sec-WebSocket-Key");
        if (key.empty()) { close(fd); return; }
        std::string accept = pilnk::wsAccept(key);
        std::string resp =
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: " + accept + "\r\n\r\n";
        if (!sendAll(fd, resp.data(), resp.size())) { close(fd); return; }

        // v2.1.0: split off a query string, so /sdr/audio?denoise=1 is still /sdr/audio.
        std::string base = path, query;
        size_t qpos = path.find('?');
        if (qpos != std::string::npos) { base = path.substr(0, qpos); query = path.substr(qpos + 1); }
        std::vector<int>* subs = nullptr;
        std::shared_ptr<DnClient> dn;
        bool dnAsked = false;
        if (base == "/sdr/fft") subs = &fftSubs;
        else if (base == "/sdr/audio") {
            dnAsked = ("&" + query + "&").find("&denoise=1&") != std::string::npos;
            if (dnAsked && dfn::api.ok) {
                std::lock_guard<std::mutex> lk(dnMtx);
                if ((int)dnSubs.size() < DN_MAX) {
                    dn = std::make_shared<DnClient>();
                    dn->fd = fd;
                    dnSubs.push_back(dn);
                    nDnSubs.store((int)dnSubs.size());
                }
            }
            if (!dn) subs = &audioSubs;   // not asked, not available, or full: plain audio (fail-soft)
        }
        else { close(fd); return; }

        // H2 fix (audit 2026-07-09): bound the send. broadcast() runs on the
        // producer (DSP/IQ) threads and drops any subscriber whose sendAll()
        // fails. Without a send timeout, a stuck/paused client (socket buffer
        // full, tab backgrounded, malicious hold) blocks that send forever,
        // stalling audio for EVERY listener and blocking sub/unsub on subsMtx.
        // SO_SNDTIMEO makes the send fail fast so the existing drop logic
        // evicts the laggard. A healthy LAN browser drains continuously and
        // never hits this; ~250ms caps the worst-case producer stall from one
        // bad client before eviction.
        struct timeval sndto{0, 250000};
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &sndto, sizeof(sndto));
        if (dn) {
            dn->worker = std::thread(&PilnkRadioDaemon::dnWorker, this, dn);
            logI("WS client subscribed to %s (denoised, %d of %d)", base.c_str(), nDnSubs.load(), DN_MAX);
        } else {
            { std::lock_guard<std::mutex> lk(subsMtx); subs->push_back(fd); refreshSubCounts(); }
            if (dnAsked) logI("WS client subscribed to %s (asked for denoise: %s - plain audio instead)",
                              base.c_str(), dfn::api.ok ? "all denoise slots in use" : dfn::api.why.c_str());
            else         logI("WS client subscribed to %s", base.c_str());
        }
        // Pongs must not interleave with data frames on the same socket. Plain listeners
        // share subsMtx with broadcast(); a denoised listener's writer is its own worker.
        std::mutex& wmx = dn ? dn->wmx : subsMtx;

        // keep alive: parse client frames properly (audit L2) — PONG the
        // PINGs so protocol-correct non-browser clients don't drop us, unmask
        // payloads, honor close. Idle timeouts (M1 SO_RCVTIMEO) at a frame
        // boundary are normal; a client stalled MID-frame gets 3 strikes.
        auto recvExact = [&](uint8_t* buf, size_t need, bool atFrameStart) -> int {
            size_t got = 0; int idleStrikes = 0;
            while (got < need && running) {
                ssize_t n = recv(fd, buf + got, need - got, 0);
                if (n > 0) { got += n; idleStrikes = 0; continue; }
                if (n == 0) return -1; // peer closed
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    if (atFrameStart && got == 0) continue;   // idle subscriber: fine forever
                    if (++idleStrikes >= 3) return -1;        // stalled mid-frame: drop
                    continue;
                }
                return -1;
            }
            return running ? (int)got : -1;
        };
        while (running) {
            uint8_t h[2];
            if (recvExact(h, 2, true) < 0) break;
            uint8_t op = h[0] & 0x0F;
            bool masked = h[1] & 0x80;
            uint64_t plen = h[1] & 0x7F;
            if (plen == 126) {
                uint8_t ext[2]; if (recvExact(ext, 2, false) < 0) break;
                plen = ((uint64_t)ext[0] << 8) | ext[1];
            } else if (plen == 127) {
                uint8_t ext[8]; if (recvExact(ext, 8, false) < 0) break;
                plen = 0; for (int i = 0; i < 8; i++) plen = (plen << 8) | ext[i];
            }
            if (plen > 4096) break; // control/client frames are tiny; oversized = protocol abuse
            uint8_t mask[4] = {0,0,0,0};
            if (masked && recvExact(mask, 4, false) < 0) break;
            std::vector<uint8_t> pay(plen);
            if (plen && recvExact(pay.data(), plen, false) < 0) break;
            if (masked) for (size_t i = 0; i < plen; i++) pay[i] ^= mask[i % 4];
            if (op == 0x8) break;         // close
            if (op == 0x9) {              // ping -> pong with same payload
                uint8_t ph[2] = { 0x8A, (uint8_t)plen };
                std::lock_guard<std::mutex> lk(wmx); // serialize with this socket's data writes
                if (!sendAll(fd, ph, 2) || (plen && !sendAll(fd, pay.data(), plen))) break;
            }
            // 0xA (pong), 0x1/0x2 (client data): ignored
        }
        if (dn) {
            // Stop the worker BEFORE closing: it may be mid-send on this fd, and a
            // closed fd number can be reused by the next connection within microseconds.
            dn->dead = true; dn->qcv.notify_all();
            if (dn->worker.joinable()) dn->worker.join();
            std::lock_guard<std::mutex> lk(dnMtx);
            auto it = std::find(dnSubs.begin(), dnSubs.end(), dn);
            if (it != dnSubs.end()) dnSubs.erase(it);
            nDnSubs.store((int)dnSubs.size());
        } else {
            std::lock_guard<std::mutex> lk(subsMtx);
            auto it = std::find(subs->begin(), subs->end(), fd);
            if (it != subs->end()) subs->erase(it);
            refreshSubCounts();
        }
        close(fd);
        logI("WS client left %s%s", base.c_str(), dn ? " (denoised)" : "");
    }

    void broadcast(std::vector<int>& subs, const uint8_t* data, size_t len) {
        // build a single binary frame (server->client, unmasked)
        uint8_t hdr[10]; size_t hl;
        hdr[0] = 0x82; // FIN + binary
        if (len < 126) { hdr[1] = (uint8_t)len; hl = 2; }
        else if (len <= 0xFFFF) { hdr[1] = 126; hdr[2] = (len >> 8) & 0xFF; hdr[3] = len & 0xFF; hl = 4; }
        else { hdr[1] = 127; for (int i = 0; i < 8; i++) hdr[2+i] = (len >> (8 * (7 - i))) & 0xFF; hl = 10; }
        std::lock_guard<std::mutex> lk(subsMtx);
        for (size_t i = 0; i < subs.size();) {
            int fd = subs[i];
            if (!sendAll(fd, hdr, hl) || (len && !sendAll(fd, data, len))) {
                close(fd); subs.erase(subs.begin() + i);
            } else { i++; }
        }
        refreshSubCounts();
    }
};

// ======================= main =======================
static std::atomic<bool> g_stop{false};
static void onSignal(int) { g_stop = true; }

int main(int argc, char** argv) {
    std::string cfgPath = "/etc/pilnkradio/config.json";
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--config" && i + 1 < argc) cfgPath = argv[++i];
        else if (a == "--version") { printf("pilnkradio %s\n", PILNKRADIO_VERSION); return 0; }
        else if (a == "--selftest") {
            Config tcfg; tcfg.path = "/dev/null";
            PilnkRadioDaemon d(tcfg);
            return d.selfTest();
        }
        else { fprintf(stderr, "usage: pilnkradio [--config <path>] [--version] [--selftest]\n"); return 2; }
    }

    logI("pilnkradio %s starting (config: %s)", PILNKRADIO_VERSION, cfgPath.c_str());
    Config cfg;
    if (!cfg.load(cfgPath)) {
        logW("config: %s not found — writing defaults", cfgPath.c_str());
        cfg.path = cfgPath;
        cfg.save();
    }
    logI("config: serial=%s vfo=%.0f mode=%s bw=%.0f gainIdx=%d ppm=%d squelch=%s@%.1f playing=%s port=%d",
         cfg.serial.c_str(), cfg.vfoHz, cfg.mode.c_str(), cfg.bandwidthHz, cfg.gainIndex, cfg.ppm,
         cfg.squelchEnabled ? "on" : "off", cfg.squelchLevel, cfg.playing ? "true" : "false", cfg.port);

    // v2.1.0: optional listener denoise. Probed HERE, before any thread exists, because
    // the probe forks — and a crash in a bad library must take down the child, not us.
    dfn::probe();

    signal(SIGINT, onSignal);
    signal(SIGTERM, onSignal);
    signal(SIGPIPE, SIG_IGN);

    PilnkRadioDaemon daemon(cfg);
    if (!daemon.start()) {
        logE("startup failed — exiting nonzero so systemd retries");
        return 1;
    }

    // Self-heal contract: any unrecoverable condition (device lost, port gone)
    // makes this process EXIT NONZERO. systemd Restart=always brings it back;
    // a udev rule restarts it on V4 replug. "Up but deaf" cannot exist.
    while (!g_stop) std::this_thread::sleep_for(std::chrono::milliseconds(200));

    logI("signal received — shutting down");
    daemon.stop();
    logI("bye");
    return 0;
}
