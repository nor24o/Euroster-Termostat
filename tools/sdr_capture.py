"""
Euroster 2006 TX - RTL-SDR 433.92 MHz RF Capture & Decoder
Listens on 433.92 MHz for OOK / Pulse Distance frames from Euroster 2006 TX.
Note: Ensure CubicSDR or other SDR apps are closed before running.
"""

import time
import sys
import os
import numpy as np

try:
    from rtlsdr import RtlSdr
except ImportError:
    print("Error: pyrtlsdr is not installed. Run: pip install pyrtlsdr")
    sys.exit(1)


def capture_rf(freq_hz=433.92e6, sample_rate=1.024e6, duration_sec=30):
    print("=" * 70)
    print(" EUROSTER 2006 TX -> RTL-SDR 433.92 MHz RF MONITOR")
    print(f" Frecvență: {freq_hz/1e6:.2f} MHz | Sample Rate: {sample_rate/1e6:.3f} MSps")
    print(f" Durată: {duration_sec} secunde")
    print("=" * 70)

    try:
        sdr = RtlSdr()
    except Exception as e:
        print(f"[EROARE DESCHIDERE RTL-SDR]: {e}")
        print("\nSFAT: Verifică dacă aplicația CubicSDR (sau alt software SDR) rulează și închide-o.")
        return

    sdr.sample_rate = sample_rate
    sdr.center_freq = freq_hz
    sdr.gain = 'auto'

    print("[SDR] Recepție activă. Ascult pe 433.92 MHz...")
    chunk_size = 256 * 1024  # ~250 ms per chunk
    t_start = time.time()

    try:
        while time.time() - t_start < duration_sec:
            samples = sdr.read_samples(chunk_size)
            # Envelope detection
            envelope = np.abs(samples)
            noise_floor = np.median(envelope)
            peak = np.max(envelope)

            if peak > noise_floor * 3.5 and peak > 0.08:
                threshold = noise_floor + (peak - noise_floor) * 0.4
                high_samples = envelope > threshold

                # Detect transitions
                diff = np.diff(high_samples.astype(np.int8))
                rising = np.where(diff == 1)[0]
                falling = np.where(diff == -1)[0]

                if len(rising) > 5 and len(falling) > 5:
                    print(f"[{time.strftime('%H:%M:%S')}] Semnal RF 433.92 MHz detectat! "
                          f"Vârf: {peak:.3f} | Zgomot: {noise_floor:.3f} | Tranziții: {len(rising)}")
            time.sleep(0.01)

    except KeyboardInterrupt:
        print("\n[INFO] Oprit de utilizator.")
    finally:
        sdr.close()
        print("[SDR] Închis.")


if __name__ == "__main__":
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    capture_rf(duration_sec=dur)
