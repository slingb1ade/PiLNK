// pilnkradio shared DSP (engine 2.3.0). One header that both the engine (main.cpp) and the
// bench replay tool (bench/radio-replay.cpp) compile, so the bench runs the radio's own code.
//
// Everything here is moved byte-for-byte from engine 2.2.0 (same expressions, same order, same
// float types): Classic must not change by even one sample (tests/dsp, tests/test_identity_230.py,
// tests/v1/selftest_cmp.sh). No locks, no I/O, no allocation in the per-sample paths.
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <complex>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace pdsp {

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

// Mean |z|² of a block of interleaved complex floats; 0 for an empty block. The squelch's
// channel power, exactly as 2.2.0 computed it inline (float accumulator, then one divide).
inline float blockPower(const float* iq, size_t n) {
    float pow = 0.0f;
    for (size_t i = 0; i < n; i++)
        pow += iq[2*i]*iq[2*i] + iq[2*i+1]*iq[2*i+1];
    if (n) pow /= (float)n;
    return pow;
}

// Channel low-pass cutoff for a channel bandwidth: half the bandwidth, kept in 1.5-20 kHz.
inline double channelCutoffHz(double bwHz) { return std::clamp(bwHz / 2.0, 1500.0, 20000.0); }

// Classic: the 2.2.0 audio chain after the channel filter. AM envelope -> one-pole DC blocker
// (~8 Hz at 48k) -> AGC (fast attack on overshoot, slow decay) -> clip to +-1.
class ClassicDemod {
public:
    void reset() { dcState = 0.0f; dcPrev = 0.0f; agcGain = 20.0f; }
    // iq: n complex samples (2n floats), the channel filter's output. out: n samples.
    void process(const float* iq, size_t n, float* out) {
        for (size_t i = 0; i < n; i++) {
            float mag = sqrtf(iq[2*i]*iq[2*i] + iq[2*i+1]*iq[2*i+1]);
            // one-pole DC blocker (~8 Hz at 48k): strips the AM carrier level
            float y = mag - dcPrev + 0.999f * dcState;
            dcPrev = mag; dcState = y;
            float s = y * agcGain;
            // AGC: fast attack on overshoot, slow decay toward target
            float a = fabsf(s);
            if (a > 0.85f)      agcGain *= 0.98f;
            else if (a < 0.15f) agcGain *= 1.0002f;
            agcGain = std::clamp(agcGain, 0.5f, 200.0f);
            out[i] = std::clamp(s, -1.0f, 1.0f);
        }
    }
    float gain() const { return agcGain; }

private:
    float dcState = 0.0f, dcPrev = 0.0f;      // DC blocker (one-pole HPF)
    float agcGain = 20.0f;
};

// ======================= Clean (engine 2.3.0) =======================
// A carrier-locked AM chain (spec "Clean"): find the carrier, track it, and while locked take
// the coherent (synchronous) detector, which gives a few dB more on weak calls than the envelope;
// level everything by the carrier itself instead of an AGC, so there is no wind-up between calls
// and no burst when a call starts. Everything is per sample; the block decisions (frequency
// step, lock) run every 480 samples and act from the next sample, so the output never depends
// on how the caller chunks its calls.
#define PDSP_HAVE_CLEAN 1

struct CleanOptions {
    enum class Detector { Envelope, Sync, Auto } detector = Detector::Auto;
    float hpfHz = 250, lpfHz = 3400;

    // "detector=auto,hpf=250,lpf=3400", every key optional. hpf 50-1000 Hz, lpf 1500-8000 Hz,
    // lpf >= hpf + 500. An unknown key or value refuses the whole string (o is left alone).
    static bool parse(const std::string& s, CleanOptions& o, std::string& err) {
        CleanOptions r;
        size_t i = 0;
        while (i < s.size()) {
            size_t j = s.find(',', i);
            if (j == std::string::npos) j = s.size();
            const std::string kv = s.substr(i, j - i);
            i = j + 1;
            if (kv.empty()) continue;
            const size_t eq = kv.find('=');
            if (eq == std::string::npos) { err = "expected key=value, got '" + kv + "'"; return false; }
            const std::string k = kv.substr(0, eq), v = kv.substr(eq + 1);
            if (k == "detector") {
                if (v == "envelope") r.detector = Detector::Envelope;
                else if (v == "sync") r.detector = Detector::Sync;
                else if (v == "auto") r.detector = Detector::Auto;
                else { err = "detector must be envelope, sync or auto"; return false; }
            } else if (k == "hpf" || k == "lpf") {
                char* e = nullptr;
                const double x = strtod(v.c_str(), &e);
                if (v.empty() || !e || *e || !std::isfinite(x)) { err = k + " must be a number"; return false; }
                if (k == "hpf") { if (x < 50 || x > 1000) { err = "hpf must be 50-1000 Hz"; return false; } r.hpfHz = (float)x; }
                else { if (x < 1500 || x > 8000) { err = "lpf must be 1500-8000 Hz"; return false; } r.lpfHz = (float)x; }
            } else { err = "unknown key '" + k + "'"; return false; }
        }
        if (r.lpfHz < r.hpfHz + 500) { err = "lpf must be at least hpf + 500 Hz"; return false; }
        o = r;
        return true;
    }
    std::string str() const {
        auto num = [](float x) { char b[32]; snprintf(b, sizeof b, "%g", (double)x); return std::string(b); };
        const char* d = detector == Detector::Envelope ? "envelope" : detector == Detector::Sync ? "sync" : "auto";
        return std::string("detector=") + d + ",hpf=" + num(hpfHz) + ",lpf=" + num(lpfHz);
    }
};

// In-place forward FFT, n a power of 2 (iterative radix-2).
inline void fftRadix2(std::complex<float>* x, size_t n) {
    for (size_t i = 1, j = 0; i < n; i++) {
        size_t bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) std::swap(x[i], x[j]);
    }
    for (size_t len = 2; len <= n; len <<= 1) {
        const double ang = -2.0 * M_PI / (double)len;
        const std::complex<double> wl(cos(ang), sin(ang));
        for (size_t i = 0; i < n; i += len) {
            std::complex<double> w(1.0, 0.0);
            for (size_t k = 0; k < len / 2; k++) {
                const std::complex<float> u = x[i + k];
                const std::complex<float> v = x[i + k + len / 2] * std::complex<float>(w);
                x[i + k] = u + v;
                x[i + k + len / 2] = u - v;
                w *= wl;
            }
        }
    }
}

// RBJ biquad (bilinear, prewarped), transposed direct form II in double.
struct Biquad {
    double b0 = 1, b1 = 0, b2 = 0, a1 = 0, a2 = 0, z1 = 0, z2 = 0;
    static Biquad make(float fs, float f, float q, bool high) {
        const double w0 = 2.0 * M_PI * f / fs, c = cos(w0), al = sin(w0) / (2.0 * q), a0 = 1.0 + al;
        Biquad r;
        if (high) { r.b0 = (1 + c) / 2 / a0; r.b1 = -(1 + c) / a0; r.b2 = (1 + c) / 2 / a0; }
        else      { r.b0 = (1 - c) / 2 / a0; r.b1 = (1 - c) / a0;  r.b2 = (1 - c) / 2 / a0; }
        r.a1 = -2 * c / a0; r.a2 = (1 - al) / a0;
        return r;
    }
    static Biquad highpass(float fs, float f, float q) { return make(fs, f, q, true); }
    static Biquad lowpass(float fs, float f, float q) { return make(fs, f, q, false); }
    float step(float x) {
        const double y = b0 * x + z1;
        z1 = b1 * x - a1 * y + z2;
        z2 = b2 * x - a2 * y;
        return (float)y;
    }
    void reset() { z1 = z2 = 0; }
};

// |y| <= T passes untouched; above it a tanh knee that approaches (never reaches) 1.
inline float softLimit(float y) {
    const float T = 0.70710678f, a = fabsf(y);
    if (a <= T) return y;
    const float v = std::min(T + (1.0f - T) * tanhf((a - T) / (1.0f - T)), 0.99999994f);  // tanhf saturates at 1
    return y < 0 ? -v : v;
}

class CleanDemod {
public:
    static constexpr double FS = 48000.0;
    static constexpr int BLK = 480;              // block decisions every 10 ms
    static constexpr int NFFT = 4096;            // acquisition FFT
    static constexpr int ACQ_EVERY = 2048;       // while there is no carrier estimate
    static constexpr int XFADE = 960;            // detector crossfade, samples
    static constexpr int MAX_HOLD_BLOCKS = 6000; // a stretch longer than 60 s never sets the hold
    // Peak guard (engine 2.3.1). The level Clean works to follows the carrier it is tracking, and that takes
    // ~100 ms to notice a change. When a stronger station keyed up over a weaker one, or a call was stronger
    // than the recent ones, the new voice arrived at many times the reference and was driven flat into the
    // limiter: the bench found 4.5-17% of real calls clipped that way (22 Nov 2026). The guard sits on the
    // filtered audio, just before the soft limiter: a peak follower (instant attack, GUARD_RELEASE_S release)
    // that turns the audio down by exactly as much as its peak stands over GUARD. Nothing an ordinary call
    // produces reaches GUARD (100% modulation is 1.0), so ordinary calls are unchanged bit for bit; past it the
    // output now tops out at softLimit(GUARD / 2) = 0.93 of full scale where it used to pin at 1.0.
    static constexpr double GUARD = 2.0;
    static constexpr double GUARD_RELEASE_S = 0.05;

    CleanDemod() { configure(CleanOptions(), 8000.0); }

    void configure(const CleanOptions& o, double bwHz) {
        opt = o;
        hp1 = Biquad::highpass((float)FS, o.hpfHz, 0.5412f); hp2 = Biquad::highpass((float)FS, o.hpfHz, 1.3066f);
        lp1 = Biquad::lowpass((float)FS, o.lpfHz, 0.5412f);  lp2 = Biquad::lowpass((float)FS, o.lpfHz, 1.3066f);
        alpha = 1.0 - exp(-2.0 * M_PI * 20.0 / FS);
        beta = 1.0 - exp(-1.0 / (0.1 * FS));
        bEnv = 1.0 - exp(-1.0 / (0.02 * FS));
        bNoise = 1.0 - exp(-1.0 / (1.0 * FS));
        bRef = 1.0 - exp(-1.0 / (0.01 * FS));
        bGuard = 1.0 - exp(-1.0 / (GUARD_RELEASE_S * FS));
        for (int k = 0; k < NFFT; k++) hann[k] = 0.5f - 0.5f * (float)cos(2.0 * M_PI * k / (NFFT - 1));
        setBandwidth(bwHz);
        txL.reserve(MAX_HOLD_BLOCKS + 1);       // allocated once, never on the DSP thread later
        fftBuf.resize(NFFT);
        pm.reserve(NFFT);
        reset();
    }
    // A bandwidth change keeps the state; the search span and the lock normaliser follow it.
    void setBandwidth(double bwHz) {
        cutoff = channelCutoffHz(bwHz);
        g = alpha / (2.0 - alpha) * FS / (2.0 * cutoff);   // noise alone reads ~0 dB
        if (hasEst) f = std::clamp(f, -0.95 * cutoff, 0.95 * cutoff);
    }
    void reset() {
        hp1.reset(); hp2.reset(); lp1.reset(); lp2.reset();
        ring.assign(NFFT, std::complex<float>(0, 0)); ringPos = 0; since = 0;
        hasEst = false; f = 0; phase = 0; c = 0; cprev = 0; havePrev = false;
        rhos = 0; lockDbV = -100; on = off = gone = 0; lockedF = false;
        L = M = Md = Lenv = Lnoise = 0; lrefV = 0; lrefInit = false; pk = 0;
        nHold = 0; holdPos = 0;
        openState = false; txSamples = 0; txBlocks = 0; txLocked = 0; txL.clear();
        blkN = 0; blkP = 0; w = 0; seeded = false;
    }

    // iq: n complex samples (2n floats), the channel filter's output; open: the squelch gate for
    // all n samples; out: n samples in [-1, 1].
    void process(const float* iq, size_t n, bool open, float* out) {
        for (size_t i = 0; i < n; i++) out[i] = step(std::complex<float>(iq[2*i], iq[2*i+1]), open);
    }

    bool locked() const { return lockedF; }
    bool hasCarrier() const { return hasEst; }
    float carrierHz() const { return (float)f; }
    float lockDb() const { return (float)lockDbV; }
    float holdLevel() const {
        if (nHold == 0) return (float)(31.6 * Lnoise);
        float m = hold[0];
        for (int k = 1; k < nHold; k++) m = std::max(m, hold[k]);
        return m;
    }
    float lref() const { return (float)lrefV; }
    float guardPeak() const { return (float)pk; }                      // above GUARD = the guard is turning the audio down
    size_t holdCandidates() const { return txL.size(); }             // tests: the open stretch's L samples
    size_t holdCandidatesCapacity() const { return txL.capacity(); }

private:
    CleanOptions opt;
    Biquad hp1, hp2, lp1, lp2;
    double alpha = 0, beta = 0, bEnv = 0, bNoise = 0, bRef = 0, bGuard = 0, cutoff = 4000, g = 1;
    std::array<float, NFFT> hann{};
    std::vector<std::complex<float>> ring, fftBuf;
    size_t ringPos = 0;
    int since = 0;
    bool hasEst = false, havePrev = false, lockedF = false, lrefInit = false, openState = false;
    double f = 0, phase = 0, rhos = 0, lockDbV = -100;
    std::complex<double> c = 0, cprev = 0;
    int on = 0, off = 0, gone = 0;
    double L = 0, M = 0, Md = 0, Lenv = 0, Lnoise = 0, lrefV = 0, pk = 0;
    float hold[8] = {0};
    int nHold = 0, holdPos = 0;
    long long txSamples = 0;
    int txBlocks = 0, txLocked = 0;
    std::vector<float> txL;
    int blkN = 0;
    double blkP = 0, w = 0;
    bool seeded = false;                     // levels taken from the first sample after a reset
    std::vector<float> pm;                   // acquire(): masked bin powers (reserved once)

    // Peak search over |f| <= 0.95 cutoff in the last NFFT inputs. True when the peak stands
    // >= 15 dB over the median; fp is then the parabolic-interpolated peak frequency.
    bool acquire(double& fp) {
        for (int k = 0; k < NFFT; k++) fftBuf[k] = ring[(ringPos + k) % NFFT] * hann[k];
        fftRadix2(fftBuf.data(), NFFT);
        const double lim = 0.95 * cutoff, df = FS / NFFT;
        pm.clear();
        int best = -1;
        float bestP = -1;
        for (int k = 0; k < NFFT; k++) {
            const double fk = (k < NFFT / 2 ? k : k - NFFT) * df;
            if (fabs(fk) > lim) continue;
            const float P = std::norm(fftBuf[k]);
            pm.push_back(P);
            if (P > bestP) { bestP = P; best = k; }
        }
        if (best < 0 || pm.empty()) return false;
        std::nth_element(pm.begin(), pm.begin() + pm.size() / 2, pm.end());
        const double med = pm[pm.size() / 2];
        if (!(med > 0) || 10.0 * log10(bestP / med) < 15.0) return false;
        double p = 0;
        if (best > 0 && best < NFFT - 1) {
            const double a = log(std::norm(fftBuf[best - 1]) + 1e-30), b = log((double)bestP + 1e-30),
                         cc = log(std::norm(fftBuf[best + 1]) + 1e-30), den = a - 2 * b + cc;
            if (den != 0) p = 0.5 * (a - cc) / den;
        }
        fp = ((best < NFFT / 2 ? best : best - NFFT) + p) * df;
        return true;
    }
    void retune(double fp) {
        f = std::clamp(fp, -0.95 * cutoff, 0.95 * cutoff); hasEst = true;
        c = 0; havePrev = false; rhos = 0; lockedF = false; on = off = gone = 0;
    }

    float step(std::complex<float> z, bool open) {
        if (!std::isfinite(z.real()) || !std::isfinite(z.imag())) { reset(); return 0.0f; }
        if (open && !openState) {               // open edge: a new call may be a new carrier
            double fp;
            if (acquire(fp) && (!hasEst || fabs(fp - f) > 30.0)) retune(fp);
            txSamples = 0; txBlocks = 0; txLocked = 0; txL.clear();
        } else if (!open && openState) {        // close edge: a good call sets the hold level
            if (txSamples >= (long long)FS && txSamples <= (long long)(60 * FS) && txLocked * 2 >= txBlocks
                && !txL.empty()) {
                std::vector<float> v = txL;
                std::nth_element(v.begin(), v.begin() + v.size() / 2, v.end());
                hold[holdPos] = v[v.size() / 2]; holdPos = (holdPos + 1) % 8; nHold = std::min(nHold + 1, 8);
            }
        }
        openState = open;
        ring[ringPos] = z; ringPos = (ringPos + 1) % NFFT;
        if (++since >= ACQ_EVERY && !hasEst) {
            since = 0;
            double fp;
            if (acquire(fp)) retune(fp);
        } else if (since >= ACQ_EVERY) since = ACQ_EVERY;

        const double mag = std::abs(std::complex<double>(z));
        if (!seeded) {                          // start level-matched: no burst after a reset
            M = Lenv = Lnoise = mag;
            seeded = true;
        }
        Lenv += bEnv * (mag - Lenv);
        M += beta * (mag - M);
        double d = 0, cm = 0;
        std::complex<double> zr = z;
        if (hasEst) {
            phase += 2.0 * M_PI * f / FS;
            if (phase > M_PI) phase -= 2.0 * M_PI; else if (phase < -M_PI) phase += 2.0 * M_PI;
            zr = std::complex<double>(z) * std::complex<double>(cos(phase), -sin(phase));
            c += alpha * (zr - c);
            cm = std::max(std::abs(c), 1e-12);
            d = (zr * std::conj(c)).real() / cm;
            Md += beta * (d - Md);
        }
        blkP += std::norm(zr);
        if (lockedF) L += beta * (std::abs(c) - L);
        else Lnoise += bNoise * (mag - Lnoise);
        const double target = lockedF ? L : std::max((double)holdLevel(), Lenv);
        if (!lrefInit) { lrefV = target; lrefInit = true; }
        else lrefV += bRef * (target - lrefV);
        const double lr = std::max(lrefV, 1e-9);
        const double vEnv = (mag - M) / lr;
        const double vSync = hasEst ? (d - Md) / lr : vEnv;
        const double wt = opt.detector == CleanOptions::Detector::Envelope ? 0.0
                        : opt.detector == CleanOptions::Detector::Sync ? (hasEst ? 1.0 : 0.0)
                        : (lockedF ? 1.0 : 0.0);
        if (w < wt) w = std::min(wt, w + 1.0 / XFADE); else if (w > wt) w = std::max(wt, w - 1.0 / XFADE);
        const double v = (1.0 - w) * vEnv + w * vSync;
        float y = lp2.step(lp1.step(hp2.step(hp1.step((float)v))));
        const double ay = fabs((double)y);                              // peak guard: see GUARD
        pk = ay > pk ? ay : pk + bGuard * (ay - pk);
        if (pk > GUARD) y = (float)(y * (GUARD / pk));
        y = std::clamp(softLimit(0.5f * y), -1.0f, 1.0f);
        if (!std::isfinite(y)) { reset(); return 0.0f; }
        if (open) txSamples++;

        if (++blkN == BLK) {                   // ---- block decisions ----
            if (hasEst) {
                if (havePrev) {
                    f += 0.3 * std::arg(c * std::conj(cprev)) * FS / (2.0 * M_PI * BLK);
                    f = std::clamp(f, -0.95 * cutoff, 0.95 * cutoff);
                }
                cprev = c; havePrev = true;
                const double P = blkP / BLK + 1e-30;
                const double rho = std::norm(c) / (g * P);
                rhos = 0.7 * rhos + 0.3 * rho;
                lockDbV = 10.0 * log10(rhos + 1e-30);
                on = lockDbV >= 12.0 ? on + 1 : 0;
                off = lockDbV < 8.0 ? off + 1 : 0;
                if (!lockedF && on >= 2) { lockedF = true; L = std::abs(c); }
                else if (lockedF && off >= 2) lockedF = false;
                gone = lockDbV < 8.0 ? gone + 1 : 0;
                if (gone >= 20) { hasEst = false; lockedF = false; since = 0; }
            } else lockDbV = -100;
            if (openState && txBlocks <= MAX_HOLD_BLOCKS) {   // past 60 s a stretch is not a call: stop counting
                txBlocks++;
                if (lockedF) { txLocked++; txL.push_back((float)L); }
                if (txBlocks > MAX_HOLD_BLOCKS) txL.clear();
            }
            blkN = 0; blkP = 0;
        }
        return y;
    }
};

} // namespace pdsp
