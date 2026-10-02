"""
Euroster 2006 TX - RTL-SDR Raw Pulse Timing Dumper
Captures the EXACT microsecond durations of all HIGH and LOW states during a transmission.
"""

import sys
import os
import time
import json
from datetime import datetime
import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    from rtlsdr import RtlSdr
except ImportError:
    print("Error: pyrtlsdr not installed.")
    sys.exit(1)


def run_raw_dumper(freq_hz=433.92e6, sample_rate=1.024e6, duration_sec=180):
    os.makedirs("captures", exist_ok=True)
    print("=" * 70)
    print(" EUROSTER 2006 TX - RTL-SDR RAW PULSE TIMING DUMPER")
    print(f" Frecvență: {freq_hz/1e6:.2f} MHz | Sample Rate: {sample_rate/1e6:.2f} MSps")
    print(f" Durată: {duration_sec} secunde")
    print("=" * 70)

    try:
        sdr = RtlSdr()
        sdr.sample_rate = sample_rate
        sdr.center_freq = freq_hz
        sdr.gain = 29.7
    except Exception as e:
        print(f"[EROARE]: {e}")
        return

    print("[SDR] În ascultare... Trimite comandă sau așteaptă keepalive-ul de 57s!\n")
    us_per_sample = 1_000_000.0 / sample_rate
    chunk_size = 512 * 1024  # 500 ms per chunk
    t_start = time.time()
    packet_id = 0

    try:
        while time.time() - t_start < duration_sec:
            samples = sdr.read_samples(chunk_size)
            env = np.abs(samples)
            noise = np.percentile(env, 50)
            peak = np.max(env)

            if peak < 0.20 or peak < noise * 3.5:
                time.sleep(0.005)
                continue

            thresh = noise + (peak - noise) * 0.4
            high = (env > thresh).astype(np.int8)
            diff = np.diff(high)
            edges = np.where(diff != 0)[0]

            if len(edges) < 25:
                time.sleep(0.005)
                continue

            # Calculate pulse durations
            durations = np.diff(edges) * us_per_sample
            levels = [high[edges[i] + 1] for i in range(len(edges) - 1)]

            # Look for pulses > 500 us
            long_pulses = [(lvl, dur) for lvl, dur in zip(levels, durations) if dur >= 400]
            if len(long_pulses) < 20:
                continue

            packet_id += 1
            t_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"\n{'='*70}")
            print(f" >>> [CADRU BRUT #{packet_id} @ {t_str}] Vârf: {peak:.2f} | Total impulsuri: {len(long_pulses)}")
            print(f"{'='*70}")

            for idx, (lvl, dur) in enumerate(long_pulses[:50]):
                state = "HIGH (RF ON) " if lvl == 1 else "LOW  (PAUZĂ) "
                bar = "#" * int(min(dur / 200, 45))
                print(f"  {idx:02d} | {state} | {dur:>6.0f} µs | {bar}")

            # Save full array to file
            fname = f"captures/raw_sdr_burst_{packet_id:03d}_{int(time.time())}.json"
            with open(fname, "w") as f:
                json.dump({
                    "packet_id": packet_id,
                    "time": t_str,
                    "peak": float(peak),
                    "pulses": [(int(l), float(d)) for l, d in long_pulses]
                }, f, indent=2)
            print(f"  [Salvat în: {fname}]\n")

    except KeyboardInterrupt:
        print("\nOprit.")
    finally:
        sdr.close()
        print("[SDR] Închis.")


if __name__ == "__main__":
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    run_raw_dumper(duration_sec=dur)
