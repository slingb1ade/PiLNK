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

using nlohmann::json;

#define PILNKRADIO_VERSION "2.0.0-m1"
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

// ======================= daemon =======================
class PilnkRadioDaemon {
public:
    PilnkRadioDaemon(Config& cfg) : cfg(cfg) {}

    // returns false if the server could not bind (port taken => refuse to
    // start, prevents an accidental sdrpp+pilnkradio double-run on :5656)
    bool start() {
        if (!startServer()) return false;
        controlThread = std::thread(&PilnkRadioDaemon::controlLoop, this);
        // milestone 2+: openDevice(); startRadio() if cfg.playing
        return true;
    }

    void stop() {
        stopping = true;
        if (controlThread.joinable()) controlThread.join();
        stopServer();
    }

    // ---- WS broadcast (called by FFT/audio producers; producers arrive in M2/M3) ----
    void broadcastFFT(const uint8_t* data, size_t len)   { fftFrameAccum.fetch_add(1, std::memory_order_relaxed); broadcast(fftSubs, data, len); }
    void broadcastAudio(const uint8_t* data, size_t len) { audioSampleAccum.fetch_add(len / sizeof(float), std::memory_order_relaxed); broadcast(audioSubs, data, len); }

private:
    Config& cfg;
    std::atomic<bool> stopping{false};

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
    };
    std::mutex statusMtx;
    StatusSnapshot status;

    void enqueue(const Command& c) {
        std::lock_guard<std::mutex> lk(cmdMtx);
        cmdQueue.push_back(c);
    }

    // -------- control thread: drain commands, refresh status, sample flow --------
    std::thread controlThread;

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
                    // milestone 2+: start/stop rtlsdr_read_async here
                    logI("control: playing -> %s", c.bval ? "true" : "false");
                    break;
                case Command::FREQ:
                    cfg.vfoHz = c.dval;
                    // milestone 2+: retune hardware + NCO
                    logI("control: vfo -> %.0f Hz", c.dval);
                    break;
                case Command::MODE:
                    cfg.mode = modeToString(c.ival);
                    break;
                case Command::BANDWIDTH:
                    cfg.bandwidthHz = c.dval;
                    // milestone 3: swap channel LPF
                    break;
                case Command::SQUELCH_MODE:
                    cfg.squelchEnabled = c.bval;
                    break;
                case Command::SQUELCH_LEVEL:
                    cfg.squelchLevel = std::clamp(c.dval, -100.0, 0.0);
                    break;
                case Command::GAIN_INDEX:
                    cfg.gainIndex = c.ival;
                    // milestone 2+: rtlsdr_set_tuner_gain
                    break;
                case Command::AGC:
                    cfg.agc = c.bval;
                    // milestone 2+: rtlsdr_set_agc_mode
                    break;
            }
        }
        cfg.save(); // persist every applied change (incl. consent)
    }

    void refreshStatus() {
        StatusSnapshot s;
        // Hardware center sits ~950 kHz below the VFO to keep the VFO clear of
        // the DC spike — same layout the SDR++ chain used. Real tuning in M2.
        s.vfoHz = cfg.vfoHz;
        s.centerHz = cfg.vfoHz - 950000.0;
        s.bandwidthHz = cfg.bandwidthHz;
        s.mode = modeFromString(cfg.mode);
        s.playing = cfg.playing;
        s.squelchEnabled = cfg.squelchEnabled;
        s.squelchLevel = (float)cfg.squelchLevel;
        s.rfGainIndex = cfg.gainIndex;
        s.rfAgc = cfg.agc;
        // s.rfGainSteps: stays empty until M2 populates it from the tuner
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

        // WebSocket upgrade?
        std::string upg = header(req, "Upgrade");
        for (auto& ch : upg) ch = tolower(ch);
        if (upg == "websocket") { handleWS(fd, req, path); return; }

        if (method == "OPTIONS") { httpResponse(fd, "204 No Content", "text/plain", ""); close(fd); return; }

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

        std::vector<int>* subs = nullptr;
        if (path == "/sdr/fft") subs = &fftSubs;
        else if (path == "/sdr/audio") subs = &audioSubs;
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
        { std::lock_guard<std::mutex> lk(subsMtx); subs->push_back(fd); }
        logI("WS client subscribed to %s", path.c_str());

        // keep alive: read client frames (close/ping) until disconnect
        uint8_t f[1024];
        while (running) {
            ssize_t n = recv(fd, f, sizeof(f), 0);
            // M1: SO_RCVTIMEO fires here for idle subscribers (normal — they
            // just sit receiving broadcasts). A timeout is EAGAIN/EWOULDBLOCK;
            // keep the connection alive. Only a real close/error ends it.
            if (n < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) continue;
                break;
            }
            if (n == 0) break; // peer closed
            uint8_t op = f[0] & 0x0F;
            if (op == 0x8) break; // close frame
            // milestone 5 (audit L2): reply PONG to PING, unmask + parse properly
        }
        { std::lock_guard<std::mutex> lk(subsMtx);
          auto it = std::find(subs->begin(), subs->end(), fd);
          if (it != subs->end()) subs->erase(it); }
        close(fd);
        logI("WS client left %s", path.c_str());
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
        else { fprintf(stderr, "usage: pilnkradio [--config <path>] [--version]\n"); return 2; }
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
