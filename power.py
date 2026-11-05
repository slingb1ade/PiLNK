"""Supply power for the PiLNK node (6 Nov 2026, spec pilnk-tasks/power-and-hwfloor-spec.md).

Why: a weak power supply on a Pi makes the USB radio dongle drop out, and from
pilnk.io that looks exactly like "no aircraft" (VERACIOUSH, node 23, day one).
The Pi firmware knows when the supply sagged; nobody was asking it.

What a Pi can tell us:
  * every model: `vcgencmd get_throttled` -> `throttled=0x...` flags
      bit 0  under-voltage now          bit 16 under-voltage since boot
      bit 1  ARM freq capped now        bit 17 ... since boot
      bit 2  throttled now              bit 18 ... since boot
      bit 3  soft temp limit now        bit 19 ... since boot
  * Pi 5 only: real input volts, `vcgencmd pmic_read_adc EXT5V_V`
  * fallback (no vcgencmd): kernel hwmon device named `rpi_volt`,
    file in0_lcrit_alarm (1 = under-voltage). It cannot say now vs since boot,
    so 1 maps to 'low'.

Rules: every read is time-boxed (2 s) and read_power() never raises — a power
probe must never be the thing that breaks a ping or the dashboard.
"""
import os
import re
import subprocess
import time

CACHE_SECONDS = 60
CMD_TIMEOUT = 2

_UV_NOW, _CAP_NOW, _THR_NOW, _TEMP_NOW = 1 << 0, 1 << 1, 1 << 2, 1 << 3
_UV_EVER, _CAP_EVER, _THR_EVER, _TEMP_EVER = 1 << 16, 1 << 17, 1 << 18, 1 << 19

_THROTTLED_RE = re.compile(r'throttled=0x([0-9a-fA-F]+)')
_EXT5V_RE = re.compile(r'EXT5V_V\s+volt\(\d+\)=([0-9.]+)V')

_cache = {'at': None, 'value': None}


def _cache_clear():
    _cache['at'] = None
    _cache['value'] = None


def parse_throttled(text):
    """`throttled=0x50005` -> flag dict; None for anything unparseable."""
    if not text:
        return None
    m = _THROTTLED_RE.search(text)
    if not m:
        return None
    v = int(m.group(1), 16)
    return {
        'uv_now': bool(v & _UV_NOW),
        'uv_ever': bool(v & _UV_EVER),
        'thr_now': bool(v & (_THR_NOW | _CAP_NOW | _TEMP_NOW)),
        'thr_ever': bool(v & (_THR_EVER | _CAP_EVER | _TEMP_EVER)),
    }


def parse_ext5v(text):
    """Pi 5 input volts from pmic_read_adc output (only the EXT5V_V rail)."""
    if not text:
        return None
    m = _EXT5V_RE.search(text)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    if not (3.0 <= v <= 6.0):
        return None
    return round(v, 2)


def power_state(throttled, volts):
    if throttled is None:
        state = 'unknown'
    elif throttled['uv_now']:
        state = 'low'
    elif throttled['uv_ever']:
        state = 'was_low'
    else:
        state = 'ok'
    out = {
        'power': state,
        'throttled': bool(throttled and (throttled['thr_now'] or throttled['thr_ever'])),
    }
    if volts is not None:
        out['volts'] = volts
    return out


def _run(argv):
    """Run a short command; raise on a non-zero exit. Timeout = CMD_TIMEOUT s."""
    p = subprocess.run(argv, capture_output=True, text=True, timeout=CMD_TIMEOUT)
    if p.returncode != 0:
        raise RuntimeError('%s exited %s' % (argv[0], p.returncode))
    return p.stdout


def _hwmon_flags(root):
    """Kernel under-voltage alarm as a throttled-style dict, or None."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for d in names:
        base = os.path.join(root, d)
        try:
            with open(os.path.join(base, 'name')) as f:
                if f.read().strip() != 'rpi_volt':
                    continue
            with open(os.path.join(base, 'in0_lcrit_alarm')) as f:
                alarm = f.read().strip() == '1'
        except OSError:
            continue
        return {'uv_now': alarm, 'uv_ever': alarm, 'thr_now': False, 'thr_ever': False}
    return None


def read_power(run=None, hwmon_root='/sys/class/hwmon', now=None):
    """Current power state (cached CACHE_SECONDS). Never raises."""
    t = time.time() if now is None else now
    if _cache['at'] is not None and t - _cache['at'] < CACHE_SECONDS:
        return dict(_cache['value'])
    run = run or _run
    try:
        flags = None
        try:
            flags = parse_throttled(run(['vcgencmd', 'get_throttled']))
        except Exception:
            flags = None
        volts = None
        if flags is not None:
            try:
                volts = parse_ext5v(run(['vcgencmd', 'pmic_read_adc', 'EXT5V_V']))
            except Exception:
                volts = None
        else:
            flags = _hwmon_flags(hwmon_root)
        value = power_state(flags, volts)
    except Exception:
        value = power_state(None, None)
    _cache['at'] = t
    _cache['value'] = value
    return dict(value)


def ping_features():
    """The keys this module adds to the ping's `features` object."""
    p = read_power()
    return {k: p[k] for k in ('power', 'throttled', 'volts') if k in p}
