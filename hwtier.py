"""Hardware tier: 'full' or 'adsb_only' (6 Nov 2026, spec power-and-hwfloor-spec.md).

Soft floor (AJ): Pi 4 (2 GB+) and Pi 5 are supported. A Pi 3 / Pi 2 / Pi 1 / Pi Zero,
or anything with under 1.5 GiB of RAM (a 1 GB Pi 4), runs PiLNK aircraft-only:
no radio engine build, no live denoise, no datalink, no STT probe.

hwtier.sh is the shell twin used by install.sh / update.sh / datalink-install.sh.
Both are tested against tests/hwtier_cases.tsv and must agree on every row.
"""
import os
import re

RAM_FLOOR_KB = 1572864          # 1.5 GiB: a 2 GB Pi 4 reports ~1.8 GiB, a 1 GB one ~0.9
_SMALL_MODELS = re.compile(r'Raspberry Pi (3|2|Zero|Compute Module 3|Model )')


def _read_model(path):
    try:
        with open(path, 'rb') as f:
            return f.read().replace(b'\x00', b'').decode('utf-8', 'replace').strip()
    except OSError:
        return ''


def _mem_kb(path):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _inputs(model_file, meminfo, env):
    env = os.environ if env is None else env
    mf = model_file or env.get('PILNK_MODEL_FILE') or '/proc/device-tree/model'
    mi = meminfo or env.get('PILNK_MEMINFO') or '/proc/meminfo'
    return _read_model(mf), _mem_kb(mi), env


def _ram_rule(mem):
    return mem is not None and mem < RAM_FLOOR_KB


def tier(model_file=None, meminfo=None, env=None):
    model, mem, env = _inputs(model_file, meminfo, env)
    if env.get('PILNK_FORCE_FULL') == '1':
        return 'full'
    if model and _SMALL_MODELS.search(model):
        return 'adsb_only'
    if _ram_rule(mem):
        return 'adsb_only'
    return 'full'


def board_label(model_file=None, meminfo=None, env=None):
    """Text for 'This is a <board>' — model without ' Rev x', or '1 GB Raspberry Pi 4'."""
    model, mem, _ = _inputs(model_file, meminfo, env)
    short = model.split(' Rev ')[0].strip()
    if _ram_rule(mem) and not (short and _SMALL_MODELS.search(short)):
        gb = max(1, round(mem / 1048576))
        family = short.split(' Model ')[0] if short else 'computer'
        return '%d GB %s' % (gb, family)
    return short or 'computer'
