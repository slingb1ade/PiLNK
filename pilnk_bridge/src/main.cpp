// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * pilnk_bridge - PiLNK native bridge module for SDR++
 *   FFT export + audio tap + HTTP/WebSocket control API, single port.
 *
 * Copyright (C) 2026 AJ McLachlan / PiLNK
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version. SDR++ is GPL-3.0; this module is
 * a derivative work and is therefore licensed GPL-3.0-or-later.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU General Public License for more details.
 */
#include <imgui.h>
#include <module.h>
#include <gui/gui.h>
#include <gui/tuner.h>
#include <signal_path/signal_path.h>
#include <core.h>
#include <radio_interface.h>
#include <utils/flog.h>
#include <json.hpp>
#include <signal_path/sink.h>
#include <dsp/stream.h>
#include <dsp/types.h>
#include <dsp/sink/handler_sink.h>
#include <dsp/buffer/packer.h>
#include <dsp/convert/stereo_to_mono.h>
#include <fftw3.h>
#include <cmath>
#include <functional>

#include <thread>
#include <mutex>
#include <atomic>
#include <deque>
#include <vector>
#include <string>
#include <cstring>
#include <cstdint>
#include <cstdio>
#include <algorithm>

#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <unistd.h>

using nlohmann::json;

#define PILNK_BRIDGE_PORT 5656
#define PILNK_FFT_SIZE    1024
#define PILNK_FFT_RATE    25.0

SDRPP_MOD_INFO{
    /* Name:         */ "pilnk_bridge",
    /* Description:  */ "PiLNK native bridge - FFT + audio + control export",
    /* Author:       */ "AJ McLachlan / PiLNK",
    /* Version:      */ 0, 4, 0,
    /* Max instances */ 1
};

// ======================= small crypto helpers (WS handshake) =======================
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
static int modeFromString(const std::string& m) {
    if (m=="NFM"||m=="FM") return RADIO_IFACE_MODE_NFM;
    if (m=="WFM") return RADIO_IFACE_MODE_WFM;
    if (m=="AM")  return RADIO_IFACE_MODE_AM;
    if (m=="DSB") return RADIO_IFACE_MODE_DSB;
    if (m=="USB") return RADIO_IFACE_MODE_USB;
    if (m=="CW")  return RADIO_IFACE_MODE_CW;
    if (m=="LSB") return RADIO_IFACE_MODE_LSB;
    if (m=="RAW") return RADIO_IFACE_MODE_RAW;
    return -1;
}
static const char* modeToString(int m) {
    switch (m) {
        case RADIO_IFACE_MODE_NFM: return "NFM"; case RADIO_IFACE_MODE_WFM: return "WFM";
        case RADIO_IFACE_MODE_AM:  return "AM";  case RADIO_IFACE_MODE_DSB: return "DSB";
        case RADIO_IFACE_MODE_USB: return "USB"; case RADIO_IFACE_MODE_CW:  return "CW";
        case RADIO_IFACE_MODE_LSB: return "LSB"; case RADIO_IFACE_MODE_RAW: return "RAW";
        default: return "";
    }
}

// ======================= audio sink (pillar 2) =======================
// Taps demodulated audio exactly like network_sink: Packer -> StereoToMono ->
// Handler<float>, then hands raw float32 mono blocks to a send callback (which
// the module wires to broadcastAudio over WS /sdr/audio).
class PilnkAudioSink : public SinkManager::Sink {
public:
    PilnkAudioSink(SinkManager::Stream* stream, std::function<void(const uint8_t*, size_t)> sendCb)
        : _stream(stream), _send(sendCb) {
        _stream->setSampleRate(48000);
        packer.init(_stream->sinkOut, 512);
        s2m.init(&packer.out);
        monoSink.init(&s2m.out, audioHandler, this);
    }
    void start() override { packer.start(); s2m.start(); monoSink.start(); }
    void stop() override { monoSink.stop(); s2m.stop(); packer.stop(); }
    void menuHandler() override {}
private:
    static void audioHandler(float* samples, int count, void* ctx) {
        PilnkAudioSink* _this = (PilnkAudioSink*)ctx;
        _this->_send((const uint8_t*)samples, (size_t)count * sizeof(float));
    }
    SinkManager::Stream* _stream;
    std::function<void(const uint8_t*, size_t)> _send;
    dsp::buffer::Packer<dsp::stereo_t> packer;
    dsp::convert::StereoToMono s2m;
    dsp::sink::Handler<float> monoSink;
};

// ======================= module =======================
class PilnkBridgeModule : public ModuleManager::Instance {
public:
    PilnkBridgeModule(std::string name) {
        this->name = name;
        gui::menu.registerEntry(name, menuHandler, this, NULL);
        startServer();
    }

    ~PilnkBridgeModule() {
        if (audioRegistered) {
            sigpath::sinkManager.unregisterSinkProvider("PiLNK");
            audioRegistered = false;
        }
        if (iqBound) {
            iqReader.stop();
            sigpath::iqFrontEnd.unbindIQStream(&iqStream);
            iqBound = false;
        }
        stopServer();
        gui::menu.removeEntry(name);
        if (fftPlan) fftwf_destroy_plan(fftPlan);
        if (fftIn) fftwf_free(fftIn);
        if (fftOut) fftwf_free(fftOut);
    }

    void postInit() override {
        // Blackman window
        window.resize(PILNK_FFT_SIZE);
        for (int i = 0; i < PILNK_FFT_SIZE; i++) {
            window[i] = (float)(0.42 - 0.5 * cos(2.0 * M_PI * i / (PILNK_FFT_SIZE - 1))
                                     + 0.08 * cos(4.0 * M_PI * i / (PILNK_FFT_SIZE - 1)));
        }
        fftIn  = fftwf_alloc_complex(PILNK_FFT_SIZE);
        fftOut = fftwf_alloc_complex(PILNK_FFT_SIZE);
        fftPlan = fftwf_plan_dft_1d(PILNK_FFT_SIZE, fftIn, fftOut, FFTW_FORWARD, FFTW_ESTIMATE);
        // Tap the wideband decimated IQ and start the reader thread
        sigpath::iqFrontEnd.bindIQStream(&iqStream);
        iqReader.init(&iqStream, iqDataHandler, this);
        iqReader.start();
        iqBound = true;
        flog::info("pilnk_bridge: FFT tap bound (size {}, ~{} fps)", PILNK_FFT_SIZE, (int)PILNK_FFT_RATE);

        // ---- audio tap (pillar 2): register PiLNK sink and route audio to it ----
        audioProvider.create = create_audio_sink;
        audioProvider.ctx = this;
        sigpath::sinkManager.registerSinkProvider("PiLNK", audioProvider);
        for (auto& sn : sigpath::sinkManager.getStreamNames()) {
            sigpath::sinkManager.setStreamSink(sn, "PiLNK");
            flog::info("pilnk_bridge: routed audio stream \"{}\" -> PiLNK sink", sn);
        }
        audioRegistered = true;
    }
    void enable() override { enabled = true; }
    void disable() override { enabled = false; }
    bool isEnabled() override { return enabled; }

    // ---- WS broadcast (called by FFT/audio producers, added in later increments) ----
    void broadcastFFT(const uint8_t* data, size_t len)   { broadcast(fftSubs, data, len); }
    void broadcastAudio(const uint8_t* data, size_t len) { broadcast(audioSubs, data, len); }

private:
    // -------- control command queue (drained on GUI thread) --------
    struct Command {
        enum Type { FREQ, MODE, BANDWIDTH, PLAYING } type;
        double dval = 0; int ival = 0; bool bval = false;
    };
    std::mutex cmdMtx;
    std::deque<Command> cmdQueue;

    struct StatusSnapshot {
        double centerHz = 0, vfoHz = 0, bandwidthHz = 0;
        int mode = -1;
        bool playing = false;
        int audioSampleRate = 48000;
        bool haveVfo = false;
    };
    std::mutex statusMtx;
    StatusSnapshot status;
    std::atomic<int> audioSampleRate{48000};

    void enqueue(const Command& c) {
        std::lock_guard<std::mutex> lk(cmdMtx);
        cmdQueue.push_back(c);
    }

    // -------- GUI-thread hook: drain commands, refresh status --------
    static void menuHandler(void* ctx) {
        PilnkBridgeModule* _this = (PilnkBridgeModule*)ctx;
        _this->processCommands();
        _this->refreshStatus();
        StatusSnapshot s;
        { std::lock_guard<std::mutex> lk(_this->statusMtx); s = _this->status; }
        ImGui::Text("PiLNK Bridge  :%d", PILNK_BRIDGE_PORT);
        ImGui::Text("vfo %.3f MHz  %s", s.vfoHz / 1e6, s.playing ? "RX" : "idle");
        ImGui::Text("fft subs %d  audio subs %d", (int)_this->fftSubs.size(), (int)_this->audioSubs.size());
    }

    void processCommands() {
        std::deque<Command> local;
        { std::lock_guard<std::mutex> lk(cmdMtx); local.swap(cmdQueue); }
        std::string vfo = gui::waterfall.selectedVFO;
        for (auto& c : local) {
            switch (c.type) {
                case Command::PLAYING:
                    gui::mainWindow.setPlayState(c.bval);
                    break;
                case Command::FREQ:
                    if (!vfo.empty()) tuner::tune(tuner::TUNER_MODE_NORMAL, vfo, c.dval);
                    break;
                case Command::MODE:
                    if (!vfo.empty() && c.ival >= 0) {
                        int m = c.ival;
                        core::modComManager.callInterface(vfo, RADIO_IFACE_CMD_SET_MODE, &m, NULL);
                    }
                    break;
                case Command::BANDWIDTH:
                    if (!vfo.empty()) {
                        float bw = (float)c.dval;
                        core::modComManager.callInterface(vfo, RADIO_IFACE_CMD_SET_BANDWIDTH, &bw, NULL);
                    }
                    break;
            }
        }
    }

    void refreshStatus() {
        StatusSnapshot s;
        s.centerHz = gui::waterfall.getCenterFrequency();
        std::string vfo = gui::waterfall.selectedVFO;
        if (!vfo.empty()) {
            s.haveVfo = true;
            s.vfoHz = s.centerHz + sigpath::vfoManager.getOffset(vfo);
            int mode = -1;
            if (core::modComManager.callInterface(vfo, RADIO_IFACE_CMD_GET_MODE, NULL, &mode)) s.mode = mode;
            float bw = 0;
            if (core::modComManager.callInterface(vfo, RADIO_IFACE_CMD_GET_BANDWIDTH, NULL, &bw)) s.bandwidthHz = bw;
        } else {
            s.vfoHz = s.centerHz;
        }
        s.playing = gui::mainWindow.isPlaying();
        s.audioSampleRate = audioSampleRate.load();
        { std::lock_guard<std::mutex> lk(statusMtx); status = s; }
        // publish FFT framing params for the IQ reader thread
        curCenterHz.store(s.centerHz);
        curSpanHz.store(sigpath::iqFrontEnd.getEffectiveSamplerate());
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
        return j.dump();
    }

    // ============================ server ============================
    std::atomic<bool> running{false};
    int listenFd = -1;
    std::thread acceptThread;

    std::mutex subsMtx;
    std::vector<int> fftSubs;
    std::vector<int> audioSubs;

    void startServer() {
        listenFd = socket(AF_INET, SOCK_STREAM, 0);
        if (listenFd < 0) { flog::error("pilnk_bridge: socket() failed"); return; }
        int one = 1;
        setsockopt(listenFd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = INADDR_ANY;
        addr.sin_port = htons(PILNK_BRIDGE_PORT);
        if (bind(listenFd, (sockaddr*)&addr, sizeof(addr)) < 0) {
            flog::error("pilnk_bridge: bind(:{}) failed", PILNK_BRIDGE_PORT);
            close(listenFd); listenFd = -1; return;
        }
        if (listen(listenFd, 8) < 0) {
            flog::error("pilnk_bridge: listen() failed");
            close(listenFd); listenFd = -1; return;
        }
        running = true;
        acceptThread = std::thread(&PilnkBridgeModule::acceptLoop, this);
        flog::info("pilnk_bridge: HTTP/WS server listening on 0.0.0.0:{}", PILNK_BRIDGE_PORT);
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
            std::thread(&PilnkBridgeModule::handleConn, this, fd).detach();
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
        int one = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
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
            if (path == "/sdr/frequency" && b.contains("hz")) {
                c.type = Command::FREQ; c.dval = b["hz"].get<double>(); enqueue(c);
            } else if (path == "/sdr/mode" && b.contains("mode")) {
                int m = modeFromString(b["mode"].get<std::string>());
                if (m < 0) ok = false;
                else { c.type = Command::MODE; c.ival = m; enqueue(c); }
            } else if (path == "/sdr/bandwidth" && b.contains("hz")) {
                c.type = Command::BANDWIDTH; c.dval = b["hz"].get<double>(); enqueue(c);
            } else if (path == "/sdr/playing" && b.contains("on")) {
                c.type = Command::PLAYING; c.bval = b["on"].get<bool>(); enqueue(c);
            } else {
                ok = false;
            }
            httpResponse(fd, ok ? "200 OK" : "400 Bad Request", "application/json",
                         ok ? "{\"ok\":true}" : "{\"ok\":false,\"error\":\"bad request\"}");
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

        { std::lock_guard<std::mutex> lk(subsMtx); subs->push_back(fd); refreshSubCounts(); }
        flog::info("pilnk_bridge: WS client subscribed to {}", path);

        // keep alive: read client frames (close/ping) until disconnect
        uint8_t f[1024];
        while (running) {
            ssize_t n = recv(fd, f, sizeof(f), 0);
            if (n <= 0) break;
            if (n >= 1) {
                uint8_t op = f[0] & 0x0F;
                if (op == 0x8) break; // close
            }
        }
        { std::lock_guard<std::mutex> lk(subsMtx);
          auto it = std::find(subs->begin(), subs->end(), fd);
          if (it != subs->end()) subs->erase(it); refreshSubCounts(); }
        close(fd);
        flog::info("pilnk_bridge: WS client left {}", path);
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

    // -------- FFT tap (pillar 1) --------
    dsp::stream<dsp::complex_t> iqStream;
    dsp::sink::Handler<dsp::complex_t> iqReader;
    bool iqBound = false;
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

    static void iqDataHandler(dsp::complex_t* data, int count, void* ctx) {
        PilnkBridgeModule* _this = (PilnkBridgeModule*)ctx;
        if (_this->nFftSubs.load() == 0) { _this->fillPos = 0; _this->skipRemaining = 0; return; }
        for (int i = 0; i < count; i++) {
            if (_this->skipRemaining > 0) { _this->skipRemaining--; continue; }
            _this->fftIn[_this->fillPos][0] = data[i].re * _this->window[_this->fillPos];
            _this->fftIn[_this->fillPos][1] = data[i].im * _this->window[_this->fillPos];
            _this->fillPos++;
            if (_this->fillPos >= PILNK_FFT_SIZE) {
                _this->computeAndBroadcastFFT();
                _this->fillPos = 0;
                double span = _this->curSpanHz.load();
                int skip = (int)(span / PILNK_FFT_RATE) - PILNK_FFT_SIZE;
                _this->skipRemaining = skip > 0 ? skip : 0;
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

    // -------- audio tap (pillar 2) --------
    SinkManager::SinkProvider audioProvider;
    bool audioRegistered = false;

    static SinkManager::Sink* create_audio_sink(SinkManager::Stream* stream, std::string streamName, void* ctx) {
        PilnkBridgeModule* _this = (PilnkBridgeModule*)ctx;
        return new PilnkAudioSink(stream, [_this](const uint8_t* d, size_t n) { _this->broadcastAudio(d, n); });
    }

    std::string name;
    bool enabled = true;
};

MOD_EXPORT void _INIT_() {}
MOD_EXPORT ModuleManager::Instance* _CREATE_INSTANCE_(std::string name) { return new PilnkBridgeModule(name); }
MOD_EXPORT void _DELETE_INSTANCE_(void* instance) { delete (PilnkBridgeModule*)instance; }
MOD_EXPORT void _END_() {}
