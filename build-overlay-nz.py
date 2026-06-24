#!/usr/bin/env python3
"""
build-overlay-nz.py  —  PiLNK national-register overlay builder (New Zealand)

Downloads the NZ CAA public aircraft register CSV and transforms it into the
aircraft-overlay.csv.gz that app.py merges on top of the global
wiedehopf/tar1090-db. This closes the "ghost" gap for NZ-registered light
aircraft (GA / microlight / glider / amateur-built) that the global DB misses.

Source : NZ CAA public aircraft register (updated ~monthly).
Output : <repo>/aircraft-overlay.csv.gz  in Mictronics ';' format -> HEX;ZK-REG;TYPE
         TYPE is left blank for v1 — the registration is what de-ghosts the
         aircraft; the icon falls back to its category default until a
         model->ICAO-designator map is added later.

Run via cron on each NZ enrichment node. Python 3 stdlib only (no pip deps).
Atomic write: a failed or partial download never corrupts the existing overlay.
"""

import csv, gzip, io, os, sys, tempfile
import requests
from datetime import datetime

CAA_CSV_URL = ("https://www.aviation.govt.nz/assets/aircraft/"
               "aircraft-register/Aircraft-Register-for-website-.csv")
OUT_PATH      = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "aircraft-overlay.csv.gz")
NZ_HEX_PREFIX = "C8"   # NZ ICAO 24-bit block is C80000-C87FFF; drops stray rows

HEX_COL = "Mode S Code HEX"
REG_COL = "Registration Mark"


def log(m):
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] {m}", flush=True)


def fetch_csv():
    # Use requests (certifi CA bundle) — matches app.py and avoids the Pi's
    # system CA-store SSL quirk that breaks stdlib urllib on some nodes.
    r = requests.get(CAA_CSV_URL, timeout=120,
                     headers={"User-Agent": "pilnk-overlay-builder"})
    r.raise_for_status()
    return r.content.decode("utf-8-sig", "replace")


def build():
    try:
        if len(sys.argv) > 1:
            src = sys.argv[1]
            log(f"reading local CSV file: {src}")
            with open(src, encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        else:
            log(f"downloading {CAA_CSV_URL}")
            text = fetch_csv()
    except Exception as e:
        log(f"ERROR: could not read CSV ({e}) — keeping existing overlay")
        return 1

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or HEX_COL not in reader.fieldnames or REG_COL not in reader.fieldnames:
        log(f"ERROR: expected columns missing — keeping existing overlay. Got: {reader.fieldnames}")
        return 1

    rows, seen, skipped = [], set(), 0
    for row in reader:
        hexc = (row.get(HEX_COL) or "").strip().upper()
        reg  = (row.get(REG_COL) or "").strip().upper()
        if len(hexc) != 6 or not all(c in "0123456789ABCDEF" for c in hexc):
            skipped += 1; continue
        if not reg:
            skipped += 1; continue
        if NZ_HEX_PREFIX and not hexc.startswith(NZ_HEX_PREFIX):
            skipped += 1; continue
        if hexc in seen:
            continue
        seen.add(hexc)
        rows.append((hexc, reg))

    if not rows:
        log("ERROR: no valid rows parsed — keeping existing overlay")
        return 1

    # Atomic write: temp file in the same dir, then os.replace
    d = os.path.dirname(OUT_PATH) or "."
    fd, tmp = tempfile.mkstemp(prefix=".overlay-", suffix=".tmp", dir=d)
    os.close(fd)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as g:
            w = csv.writer(g, delimiter=";")
            for hexc, reg in rows:
                w.writerow([hexc, reg, ""])      # HEX ; ZK-REG ; TYPE(blank)
        os.replace(tmp, OUT_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    size = os.path.getsize(OUT_PATH)
    log(f"wrote {len(rows):,} NZ aircraft ({skipped:,} rows skipped) -> {OUT_PATH} ({size:,} bytes)")
    log("restart the pilnk service to load it now, or it merges on the next DB refresh.")
    return 0


if __name__ == "__main__":
    sys.exit(build())
