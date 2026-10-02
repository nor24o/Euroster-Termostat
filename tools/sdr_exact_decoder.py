"""
Euroster 2006 TX - High Precision RTL-SDR Pulse & Frame Decoder
Aligns frames strictly on the ~9 ms sync gap (7000 us - 12000 us LOW).
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


def run_precision_decoder(duration_sec=120, freq_hz=433.92e6, sample_rate=1.024e6):
    os.makedirs("captures", exist_ok=True)
    print("=" * 70)
    print(" EUROSTER 2006 TX - RTL-SDR PRECISION FRAME DECODER")
    print(f" Frecvență: {freq_hz/1e6:.2f} MHz | Sample Rate: {sample_rate/1e6:.2f} MSps")
    print(f" Durată sesiune: {duration_sec} secunde")
    print("=" * 70)

    try:
        sdr = RtlSdr()
        sdr.sample_rate = sample_rate
        sdr.center_freq = freq_hz
        sdr.gain = 29.7
    except Exception as e:
        print(f"[EROARE] RTL-SDR nu a putut fi deschis: {e}")
        return

    print("[SDR] În ascultare... Trimite comenzi de la termostat!\n")
    us_per_sample = 1_000_000.0 / sample_rate
    chunk_size = 512 * 1024  # 0.5 secunde per chunk
    t_start = time.time()
    packet_count = 0

    try:
        while time.time() - t_start < duration_sec:
            samples = sdr.read_samples(chunk_size)
            env = np.abs(samples)
            noise = np.percentile(env, 50)
            peak = np.max(env)

            if peak < 0.15 or peak < noise * 3.5:
                time.sleep(0.005)
                continue

            # Threshold for OOK slicing
            thresh = noise + (peak - noise) * 0.4
            high = (env > thresh).astype(np.int8)

            # Find run-lengths (durations of consecutive highs and lows)
            diff = np.diff(high)
            edges = np.where(diff != 0)[0]

            if len(edges) < 20:
                time.sleep(0.005)
                continue

            # Calculate durations and levels
            # edges gives indices of transitions
            intervals = np.diff(edges) * us_per_sample
            # Initial state
            levels = [high[edges[i] + 1] for i in range(len(edges) - 1)]

            # Look for SYNC pauses (LOW level for 6.5 ms to 13 ms)
            sync_indices = []
            for i, (lvl, dur) in enumerate(zip(levels, intervals)):
                if lvl == 0 and 6500 <= dur <= 14000:
                    sync_indices.append(i)

            if not sync_indices:
                continue

            timestamp_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]

            for s_idx in sync_indices:
                # The frame follows this sync pause
                # Extract pulses after sync until next sync or max 60 pulses
                frame_pulses = []
                for j in range(s_idx + 1, min(s_idx + 60, len(levels))):
                    if levels[j] == 0 and intervals[j] >= 6500:
                        break  # Next sync reached
                    frame_pulses.append((levels[j], intervals[j]))

                if len(frame_pulses) < 20:
                    continue

                # In Euroster:
                # Each bit consists of a HIGH pulse followed by a LOW pulse:
                # Short HIGH (~1000 us) + Short LOW (~1000 us) => Bit 0
                # Long HIGH  (~2000 us) + Long LOW  (~2000 us) => Bit 1
                high_durations = [dur for lvl, dur in frame_pulses if lvl == 1]
                low_durations  = [dur for lvl, dur in frame_pulses if lvl == 0]

                bits = []
                for dur in high_durations:
                    if 600 <= dur <= 1450:
                        bits.append(0)
                    elif 1550 <= dur <= 2600:
                        bits.append(1)
                    else:
                        bits.append(-1)  # Invalid

                if len(bits) >= 24 and (-1 not in bits[:24]):
                    packet_count += 1
                    id_bits = bits[:20]
                    cmd_bits = bits[20:24]

                    id_hex = 0
                    for b in id_bits:
                        id_hex = (id_hex << 1) | b

                    cmd_val = 0
                    for b in cmd_bits:
                        cmd_val = (cmd_val << 1) | b

                    cmd_name = "NECUNOSCUT"
                    if cmd_val == 0b1000:
                        cmd_name = "HEAT ON  (Pornit)"
                    elif cmd_val == 0b0100:
                        cmd_name = "HEAT OFF (Oprit)"

                    bit_str = "".join(str(b) for b in bits[:24])

                    print(f"[{timestamp_str}] [PACHET #{packet_count:02d}] "
                          f"ID: 0x{id_hex:05X} | CMD: 0b{cmd_val:04b} ({cmd_name}) | "
                          f"Biți: {bit_str}")
                    print(f"    Avg HIGH Bit0: {np.mean([d for d in high_durations[:24] if d < 1500]):.0f} us | "
                          f"Avg HIGH Bit1: {np.mean([d for d in high_durations[:24] if d >= 1500] or [0]):.0f} us")

                    # Save to file
                    filename = f"captures/sdr_frame_{packet_count:03d}_{int(time.time())}.json"
                    with open(filename, "w") as f:
                        json.dump({
                            "time": timestamp_str,
                            "id_hex": f"0x{id_hex:05X}",
                            "id_val": id_hex,
                            "cmd_val": cmd_val,
                            "cmd_name": cmd_name,
                            "bits": bit_str,
                            "high_durations_us": [round(float(d), 1) for d in high_durations[:25]],
                            "low_durations_us": [round(float(d), 1) for d in low_durations[:25]]
                        }, f, indent=2)

    except KeyboardInterrupt:
        print("\nOprit.")
    finally:
        sdr.close()
        print(f"\n[FINALIZAT] Total cadre decodificate cu succes: {packet_count}")


if __name__ == "__main__":
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    run_precision_decoder(duration_sec=dur)
