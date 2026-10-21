#!/usr/bin/env python3
"""Fix c17.glb (AJ, 20 Oct 2026): its root node 'Sketchfab_model' carries the author's scene
rotation - a 55.75 deg spin about the vertical plus an offset - on top of the usual Z-up to Y-up
turn. The aircraft inside is square (engines in mirrored pairs along X), so the spin is the whole
fault. This keeps the Z-up to Y-up turn and drops the spin and the offset (the pages recentre every
model on its box anyway). The BIN chunk is copied byte for byte.
Usage: fix_c17.py IN.glb OUT.glb"""
import json, struct, sys

def main(src, dst):
    b = open(src, 'rb').read()
    magic, ver, total = struct.unpack('<III', b[:12])
    assert magic == 0x46546C67 and ver == 2, 'not a glTF 2 binary'
    jlen, jtype = struct.unpack('<II', b[12:20])
    assert jtype == 0x4E4F534A, 'first chunk is not JSON'
    j = json.loads(b[20:20 + jlen])
    rest = b[20 + jlen:]                      # BIN chunk header + data, untouched
    root = j['scenes'][j.get('scene', 0)]['nodes']
    assert root == [0], 'expected one root node'
    n0 = j['nodes'][0]
    assert n0.get('name') == 'Sketchfab_model' and 'matrix' in n0, 'root node is not the one this fixes'
    m = n0['matrix']                          # column-major
    # It must be exactly (spin about vertical) x (Z-up to Y-up): Z column straight up, X/Y columns flat.
    assert abs(m[8]) < 1e-6 and abs(m[9] - 1) < 1e-6 and abs(m[10]) < 1e-6, 'Z column is not straight up'
    assert abs(m[1]) < 1e-6 and abs(m[5]) < 1e-6, 'X/Y columns are not level'
    n0['matrix'] = [1, 0, 0, 0,  0, 0, -1, 0,  0, 1, 0, 0,  0, 0, 0, 1]   # Z-up to Y-up only
    js = json.dumps(j, separators=(',', ':')).encode()
    js += b' ' * ((4 - len(js) % 4) % 4)      # chunks are 4-byte aligned, JSON pads with spaces
    out = struct.pack('<III', magic, ver, 12 + 8 + len(js) + len(rest)) + struct.pack('<II', len(js), jtype) + js + rest
    open(dst, 'wb').write(out)

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
