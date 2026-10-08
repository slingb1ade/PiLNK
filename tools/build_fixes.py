# Build static/fixes-nz.js from FlightGear/X-Plane fix.dat.gz (GPL v2+, Robin A. Peel, cycle 2013.10).
# Keeps the source's copyright/licence header (the GPL asks for it), filters to NZ, writes ASCII JS.
# Usage (8 Oct 2026): download Navaids/fix.dat.gz from https://gitlab.com/flightgear/fgdata (branch next)
# into the current folder, then:  python3 tools/build_fixes.py static/fixes-nz.js
import gzip, re, sys, textwrap
raw = gzip.open('fix.dat.gz').read().decode('latin-1').replace('\r', '')
lines = raw.split('\n')
header = lines[1].strip().replace('\xa9', '(c)')
assert 'GNU General Public License' in header and 'Robin A. Peel' in header
fixes = []
for ln in lines[2:]:
    p = ln.split()
    if len(p) != 3: continue
    lat, lon, name = float(p[0]), float(p[1]), p[2]
    if -48 < lat < -33 and (lon > 165 or lon < -175) and re.match(r'^[A-Z0-9]{2,5}$', name):
        fixes.append((name, lat, lon))
fixes.sort()
assert len({f[0] for f in fixes}) == len(fixes)
wrapped = '\n'.join(' * ' + l for l in textwrap.wrap(header, 110))
out = ('/* PiLNK - New Zealand IFR fixes for the dashboard "IFR Fixes" layer (8 Oct 2026).\n'
       ' *\n'
       ' * Source: the X-Plane / FlightGear navigation data, file Navaids/fix.dat.gz in FlightGear fgdata\n'
       ' * (https://gitlab.com/flightgear/fgdata), filtered to New Zealand (lat -33 to -48, lon east of 165E\n'
       ' * or west of 175W) by build_fixes.py. ' + str(len(fixes)) + ' fixes. Format: [name, lat, lon].\n'
       ' * The data is from 2013: fixes added since (ANSER, for one) are missing, and some may have moved or\n'
       ' * been withdrawn. For watching aircraft only - NOT FOR NAVIGATION.\n'
       ' *\n'
       ' * Licence: this file is distributed under the GNU General Public License, version 2 or later, as\n'
       ' * the source data is. Full text: fixes-nz.GPL-2.0.txt next to this file. Original header, kept\n'
       ' * intact (the copyright sign written as (c) to keep the file ASCII):\n'
       ' *\n' + wrapped + '\n */\n'
       'window.IFR_FIXES_NZ = [\n' +
       ',\n'.join('["%s",%s,%s]' % (n, repr(la), repr(lo)) for n, la, lo in fixes) +
       '\n];\n')
assert all(ord(c) < 128 for c in out) and '\\' not in out
open(sys.argv[1] if len(sys.argv) > 1 else 'fixes-nz.js', 'w').write(out)
print(len(fixes), len(out))
