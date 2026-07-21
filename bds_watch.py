import socket
import pyModeS as pms

print(f"pyModeS version: {pms.__version__}")
print("Connecting to dump1090-fa on port 30002...")
s = socket.socket()
s.connect(('127.0.0.1', 30002))
print("Connected. Using pyModeS v3 decode() API...\n")

buf = ''
total = 0
df_counts = {}
hits = 0

while True:
    buf += s.recv(4096).decode(errors='ignore')
    while '\n' in buf:
        line, buf = buf.split('\n', 1)
        msg = line.strip().lstrip('*').rstrip(';').strip()
        if len(msg) < 14:
            continue
        total += 1

        try:
            first_byte = int(msg[0:2], 16)
            df = first_byte >> 3
            df_counts[df] = df_counts.get(df, 0) + 1

            # BDS registers come from DF20/21 Comm-B replies
            if df in [20, 21] and len(msg) == 28:
                try:
                    result = pms.decode(msg)
                    if result:
                        # Wind / temperature (BDS44)
                        if 'wind_speed' in result or 'temperature' in result:
                            hits += 1
                            print(f"🌬️  BDS44 MET REPORT #{hits}")
                            print(f"   wind_speed   : {result.get('wind_speed')} kt")
                            print(f"   wind_direction: {result.get('wind_direction')}°")
                            print(f"   temperature  : {result.get('temperature')} C")
                            print(f"   raw: {msg}\n")
                        # Turbulence / hazard (BDS45)
                        elif 'turbulence' in result:
                            hits += 1
                            print(f"⚡ BDS45 MET HAZARD #{hits} | turbulence={result.get('turbulence')} | {msg}")
                        # Pilot selected altitude (BDS40)
                        elif 'selected_altitude' in result or 'fms_altitude' in result:
                            hits += 1
                            print(f"✈️  BDS40 PILOT INTENT #{hits} | alt={result.get('selected_altitude') or result.get('fms_altitude')} ft | {msg}")
                        else:
                            # Show all fields from any DF20/21 decode
                            filtered = {k: v for k, v in result.items() if v is not None}
                            if filtered:
                                print(f"   DF{df} decoded: {filtered}")
                except Exception:
                    pass
        except Exception:
            pass

        if total % 500 == 0:
            print(f"[{total} msgs] DFs: {dict(sorted(df_counts.items()))} | MET hits: {hits}")
