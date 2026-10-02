"""
Euroster 2006 TX - Dual Hardware Capture & Protocol Analyzer
Simultaneously captures:
  1. PPK2 (Logic Analyzer on COM13):
       - D0: RESET pin to PIC TX
       - D1: ON pin
       - D2: OFF pin
  2. RTL-SDR (RF Receiver on 433.92 MHz):
       - OOK / Pulse Distance demodulator with noise-reject filter
       - Decodes 20-bit ID + 4-bit Command + Sync timing
"""

import sys
import os
import time
import json
import threading
from datetime import datetime
import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

print_lock = threading.Lock()

def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)
        sys.stdout.flush()


# =====================================================================
# PPK2 Logic Monitor
# =====================================================================
def run_ppk2_worker(port, stop_event, output_dir):
    try:
        import serial
        from ppk2_api.ppk2_api import PPK2_API, PPK2_Command
    except ImportError:
        safe_print("[PPK2] EROARE: ppk2-api sau pyserial nu sunt instalate.")
        return

    try:
        s = serial.Serial(port, 9600, timeout=0.2)
        s.write(bytes([PPK2_Command.AVERAGE_STOP]))
        time.sleep(0.1)
        s.reset_input_buffer()
        s.close()
    except Exception:
        pass

    try:
        ppk2 = PPK2_API(port)
        def safe_read_metadata(self):
            for _ in range(0, 10):
                read = self.ser.read(self.ser.in_waiting)
                time.sleep(0.1)
                decoded = read.decode('utf-8', errors='ignore')
                if 'END' in decoded:
                    return decoded
            return ''
        ppk2._read_metadata = safe_read_metadata.__get__(ppk2, PPK2_API)
        ppk2.get_modifiers()
        ppk2.use_ampere_meter()
        ppk2.set_source_voltage(3300)
    except Exception as e:
        safe_print(f"[PPK2] Eroare conectare pe {port}: {e}")
        return

    ppk2.start_measuring()
    safe_print(f"[PPK2] Conectat si activ pe {port} (100 kSps)")

    time.sleep(0.05)
    init_data = ppk2.get_data()
    last_state = (0, 0, 0)
    if init_data:
        _, raw_init = ppk2.get_samples(init_data)
        if raw_init:
            last_byte = raw_init[-1]
            last_state = ((last_byte >> 0) & 1, (last_byte >> 1) & 1, (last_byte >> 2) & 1)
            safe_print(f"[PPK2 INITIAL] D0(Reset)={last_state[0]}, D1(ON)={last_state[1]}, D2(OFF)={last_state[2]}")

    last_change_time = time.time()
    current_burst = []
    burst_start_time = None
    burst_id = 0

    try:
        while not stop_event.is_set():
            data = ppk2.get_data()
            if not data:
                time.sleep(0.005)
                if current_burst and (time.time() - last_change_time > 0.15):
                    burst_duration_ms = (last_change_time - burst_start_time) * 1000
                    burst_id += 1
                    handle_ppk2_burst(current_burst, burst_id, burst_duration_ms, output_dir)
                    current_burst = []
                    burst_start_time = None
                continue

            samples, raw_digital = ppk2.get_samples(data)
            if not raw_digital:
                continue

            for val in raw_digital:
                d0 = (val >> 0) & 1
                d1 = (val >> 1) & 1
                d2 = (val >> 2) & 1
                curr_state = (d0, d1, d2)

                if curr_state != last_state:
                    now = time.time()
                    dt_us = int((now - last_change_time) * 1_000_000)
                    t_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]

                    label_parts = []
                    if curr_state[0] != last_state[0]:
                        label_parts.append(f"D0(RESET)={'HIGH' if d0 else 'LOW'}")
                    if curr_state[1] != last_state[1]:
                        label_parts.append(f"D1(ON)={'HIGH' if d1 else 'LOW'}")
                    if curr_state[2] != last_state[2]:
                        label_parts.append(f"D2(OFF)={'HIGH' if d2 else 'LOW'}")
                    label = ", ".join(label_parts)

                    if not current_burst:
                        burst_start_time = now

                    event = {
                        "t": now,
                        "t_str": t_str,
                        "dt_us": dt_us,
                        "d0": d0,
                        "d1": d1,
                        "d2": d2,
                        "label": label
                    }
                    current_burst.append(event)
                    safe_print(f"[PPK2 {t_str}] D0={d0} D1={d1} D2={d2} | dt={dt_us:>7} us | {label}")

                    last_state = curr_state
                    last_change_time = now

    finally:
        if current_burst:
            burst_duration_ms = (last_change_time - burst_start_time) * 1000
            burst_id += 1
            handle_ppk2_burst(current_burst, burst_id, burst_duration_ms, output_dir)
        try:
            ppk2.stop_measuring()
            ppk2.ser.close()
        except Exception:
            pass
        safe_print("[PPK2] Oprit.")


def handle_ppk2_burst(burst, burst_id, duration_ms, output_dir):
    d0_toggles = sum(1 for e in burst if "D0" in e["label"])
    d1_toggles = sum(1 for e in burst if "D1" in e["label"])
    d2_toggles = sum(1 for e in burst if "D2" in e["label"])

    intent = "PULS NECUNOSCUT"
    if d1_toggles > 0 and d2_toggles == 0:
        intent = "COMANDĂ TERMOSTAT: HEAT ON (Pornire căldură)"
    elif d2_toggles > 0 and d1_toggles == 0:
        intent = "COMANDĂ TERMOSTAT: HEAT OFF (Oprire căldură)"
    elif d1_toggles > 0 and d2_toggles > 0:
        intent = "AMBELE LINII D1 & D2 ACTIVE"
    elif d0_toggles > 0:
        intent = "DOAR IMPULS D0 (RESET)"

    safe_print(f"\n=======================================================")
    safe_print(f" >>> [PPK2 SECVENȚĂ #{burst_id}] {intent}")
    safe_print(f"     Tranziții: {len(burst)} (D0:{d0_toggles}, D1:{d1_toggles}, D2:{d2_toggles}) | Durată: {duration_ms:.2f} ms")
    safe_print(f"=======================================================\n")

    filename = os.path.join(output_dir, f"ppk2_burst_{burst_id:03d}_{int(time.time())}.json")
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump({"id": burst_id, "intent": intent, "events": burst}, f, indent=2)
    except Exception:
        pass


# =====================================================================
# RTL-SDR 433.92 MHz Monitor & Demodulator
# =====================================================================
def run_sdr_worker(freq_hz, sample_rate, stop_event, output_dir):
    try:
        from rtlsdr import RtlSdr
    except ImportError:
        safe_print("[SDR] EROARE: pyrtlsdr nu este instalat.")
        return

    try:
        sdr = RtlSdr()
        sdr.sample_rate = sample_rate
        sdr.center_freq = freq_hz
        sdr.gain = 25.0
    except Exception as e:
        safe_print(f"[SDR] Nu s-a putut deschide RTL-SDR: {e}")
        return

    safe_print(f"[SDR] Conectat si activ pe {freq_hz/1e6:.2f} MHz (Rate: {sample_rate/1e6:.2f} MSps)")
    chunk_size = 256 * 1024  # ~256 ms per chunk
    us_per_sample = 1_000_000.0 / sample_rate

    try:
        while not stop_event.is_set():
            samples = sdr.read_samples(chunk_size)
            envelope = np.abs(samples)
            noise_floor = np.percentile(envelope, 50)
            peak = np.max(envelope)

            # Require clear signal above noise
            if peak > 0.15 and peak > noise_floor * 4:
                threshold = noise_floor + (peak - noise_floor) * 0.45
                high_samples = envelope > threshold

                # Edge transitions
                diff = np.diff(high_samples.astype(np.int8))
                edge_indices = np.where(diff != 0)[0]

                if len(edge_indices) >= 10:
                    durations_us = np.diff(edge_indices) * us_per_sample
                    first_level = high_samples[edge_indices[0]]

                    # Clean filter: Keep pulses with duration > 400 us
                    valid_pulses = []
                    for idx, dur in enumerate(durations_us):
                        level = (first_level + idx) % 2  # 1 for HIGH, 0 for LOW
                        if dur >= 400:
                            valid_pulses.append((level, dur))

                    # Check if we have euroster-like pulses (e.g. sync ~9ms, bits ~1ms or ~2ms)
                    euroster_pulses = [p for p in valid_pulses if (600 <= p[1] <= 2600) or (6000 <= p[1] <= 14000)]
                    if len(euroster_pulses) >= 15:
                        t_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        analyze_euroster_rf(euroster_pulses, t_str, peak, output_dir)

            time.sleep(0.005)
    finally:
        try:
            sdr.close()
        except Exception:
            pass
        safe_print("[SDR] Oprit.")


def analyze_euroster_rf(pulses, t_str, peak, output_dir):
    """
    Decodes pulses into bits:
    Sync: LOW ~9000 us
    Bit 0: HIGH ~1000 us, LOW ~1000 us
    Bit 1: HIGH ~2000 us, LOW ~2000 us
    """
    # Count sync gaps
    syncs = [p for p in pulses if p[0] == 0 and 6000 <= p[1] <= 13000]

    # Classify HIGH pulses:
    # ~1000 us -> 0, ~2000 us -> 1
    high_pulses = [p[1] for p in pulses if p[0] == 1]
    bits = []
    for dur in high_pulses:
        if 600 <= dur <= 1450:
            bits.append(0)
        elif 1550 <= dur <= 2600:
            bits.append(1)

    bit_string = "".join(str(b) for b in bits)

    safe_print(f"\n=======================================================")
    safe_print(f" >>> [SDR RF 433.92 MHz CADRU DETECTAT @ {t_str}]")
    safe_print(f"     Vârf semnal: {peak:.3f} | Total pulsi RF: {len(pulses)} | Pauze Sync: {len(syncs)}")
    if len(bits) >= 20:
        safe_print(f"     Biți decodificați ({len(bits)} biți): {bit_string[:30]}...")

        # If we have at least 24 bits:
        if len(bits) >= 24:
            # First 20 bits ID, next 4 bits CMD
            frame_bits = bits[:24]
            id_val = 0
            for b in frame_bits[:20]:
                id_val = (id_val << 1) | b
            cmd_val = 0
            for b in frame_bits[20:24]:
                cmd_val = (cmd_val << 1) | b

            cmd_name = "NECUNOSCUT"
            if cmd_val == 0b1000:
                cmd_name = "HEAT ON (Pornire Căldură)"
            elif cmd_val == 0b0100:
                cmd_name = "HEAT OFF (Oprire Căldură)"

            safe_print(f"     >>> DECODARE PACHET: ID=0x{id_val:X} | CMD=0b{cmd_val:04b} ({cmd_name}) <<<")
    safe_print(f"=======================================================\n")


# =====================================================================
# Main Coordinator
# =====================================================================
def main(duration_sec=300):
    output_dir = "captures"
    os.makedirs(output_dir, exist_ok=True)

    safe_print("=" * 75)
    safe_print(" EUROSTER 2006 TX - CAPTURĂ DUALĂ HARDWARE (PPK2 + RTL-SDR)")
    safe_print("   PPK2: Monitorizează digital D0(Reset), D1(ON), D2(OFF) pe COM13")
    safe_print("   RTL-SDR: Monitorizează RF 433.92 MHz OOK de pe antenă")
    safe_print(f"   Durată sesiune: {duration_sec} secunde")
    safe_print("=" * 75)
    safe_print("\n>>> AMBELE SISTEME SUNT ACTIVE! APASĂ BUTOANELE DE PE TERMOSTAT ACUM! <<<\n")

    stop_event = threading.Event()

    t_ppk2 = threading.Thread(target=run_ppk2_worker, args=("COM13", stop_event, output_dir), daemon=True)
    t_sdr = threading.Thread(target=run_sdr_worker, args=(433.92e6, 1.024e6, stop_event, output_dir), daemon=True)

    t_ppk2.start()
    t_sdr.start()

    try:
        t_start = time.time()
        while time.time() - t_start < duration_sec:
            time.sleep(0.5)
    except KeyboardInterrupt:
        safe_print("\n[INFO] Oprire manuală solicitată.")
    finally:
        stop_event.set()
        time.sleep(1.0)
        safe_print("[INFO] Captură duală încheiată.")


if __name__ == "__main__":
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    main(dur)
