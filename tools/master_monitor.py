"""
Euroster 2006 TX - Master Dual Monitor (PPK2 + RTL-SDR)
Simultaneously monitors:
  - PPK2: D0 (Reset), D1 (ON), D2 (OFF), D3 (TX DATA)
  - RTL-SDR: 433.92 MHz RF Carrier & Frames
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

def log(msg):
    with print_lock:
        print(msg)
        sys.stdout.flush()


# =====================================================================
# PPK2 Logic Monitor (D0..D3)
# =====================================================================
def ppk2_thread(port, stop_event):
    try:
        import serial
        from ppk2_api.ppk2_api import PPK2_API, PPK2_Command
    except ImportError:
        log("[PPK2] EROARE: ppk2-api lipsă.")
        return

    # Flush port
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
        log(f"[PPK2] Eroare conectare {port}: {e}")
        return

    ppk2.start_measuring()
    log("[PPK2] Conectat și activ pe COM13 (D0=Reset, D1=ON, D2=OFF, D3=DATA)")

    time.sleep(0.05)
    init_data = ppk2.get_data()
    last_state = (1, 0, 0, 0)
    if init_data:
        _, raw = ppk2.get_samples(init_data)
        if raw:
            b = raw[-1]
            last_state = ((b >> 0) & 1, (b >> 1) & 1, (b >> 2) & 1, (b >> 3) & 1)
            log(f"[PPK2 START] Nivel curent: D0={last_state[0]}, D1={last_state[1]}, D2={last_state[2]}, D3={last_state[3]}")

    last_change_time = time.time()
    burst_events = []
    burst_start = None

    try:
        while not stop_event.is_set():
            data = ppk2.get_data()
            if not data:
                time.sleep(0.005)
                if burst_events and (time.time() - last_change_time > 0.2):
                    dur_ms = (last_change_time - burst_start) * 1000
                    summarize_ppk2_burst(burst_events, dur_ms)
                    burst_events = []
                    burst_start = None
                continue

            samples, raw_digital = ppk2.get_samples(data)
            if not raw_digital:
                continue

            for val in raw_digital:
                d0 = (val >> 0) & 1
                d1 = (val >> 1) & 1
                d2 = (val >> 2) & 1
                d3 = (val >> 3) & 1
                curr_state = (d0, d1, d2, d3)

                if curr_state != last_state:
                    now = time.time()
                    dt_us = int((now - last_change_time) * 1_000_000)
                    t_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]

                    # Detect what changed
                    changes = []
                    if curr_state[0] != last_state[0]: changes.append(f"D0(Reset)->{d0}")
                    if curr_state[1] != last_state[1]: changes.append(f"D1(ON)->{d1}")
                    if curr_state[2] != last_state[2]: changes.append(f"D2(OFF)->{d2}")
                    if curr_state[3] != last_state[3]: changes.append(f"D3(DATA)->{d3}")

                    # If control lines (D0, D1, D2) change, print immediately
                    if curr_state[0] != last_state[0] or curr_state[1] != last_state[1] or curr_state[2] != last_state[2]:
                        log(f" >>> [PPK2 CONTROL {t_str}] {', '.join(changes)} | D0={d0} D1={d1} D2={d2} | dt={dt_us} us")

                    if not burst_events:
                        burst_start = now

                    burst_events.append({
                        "t": now, "t_str": t_str, "dt_us": dt_us,
                        "d0": d0, "d1": d1, "d2": d2, "d3": d3,
                        "label": ", ".join(changes)
                    })

                    last_state = curr_state
                    last_change_time = now

    finally:
        try:
            ppk2.stop_measuring()
            ppk2.ser.close()
        except Exception:
            pass
        log("[PPK2] Închis.")


def summarize_ppk2_burst(events, dur_ms):
    d0_c = sum(1 for e in events if "D0" in e["label"])
    d1_c = sum(1 for e in events if "D1" in e["label"])
    d2_c = sum(1 for e in events if "D2" in e["label"])
    d3_c = sum(1 for e in events if "D3" in e["label"])

    t_str = events[0]["t_str"]
    cmd_label = ""
    if d1_c > 0 and d2_c == 0: cmd_label = " [COMANDĂ MCU: HEAT ON!]"
    elif d2_c > 0 and d1_c == 0: cmd_label = " [COMANDĂ MCU: HEAT OFF!]"
    elif d0_c > 0: cmd_label = " [RESETARE MCU (D0 pulse)]"

    log(f"[PPK2 BURST @ {t_str}]{cmd_label} Durată: {dur_ms:.1f} ms | Tranziții: D0:{d0_c}, D1:{d1_c}, D2:{d2_c}, D3(DATA):{d3_c}")


# =====================================================================
# RTL-SDR 433.92 MHz Receiver
# =====================================================================
def sdr_thread(freq_hz, sample_rate, stop_event):
    try:
        from rtlsdr import RtlSdr
    except ImportError:
        log("[SDR] EROARE: pyrtlsdr lipsă.")
        return

    try:
        sdr = RtlSdr()
        sdr.sample_rate = sample_rate
        sdr.center_freq = freq_hz
        sdr.gain = 29.7
    except Exception as e:
        log(f"[SDR] Nu s-a putut deschide RTL-SDR: {e}")
        return

    log(f"[SDR] Conectat și activ pe {freq_hz/1e6:.2f} MHz")
    chunk_size = 256 * 1024
    us_per_sample = 1_000_000.0 / sample_rate

    try:
        while not stop_event.is_set():
            samples = sdr.read_samples(chunk_size)
            env = np.abs(samples)
            noise = np.percentile(env, 50)
            peak = np.max(env)

            if peak > 0.15 and peak > noise * 3.5:
                thresh = noise + (peak - noise) * 0.4
                high = (env > thresh).astype(np.int8)
                diff = np.diff(high)
                edges = np.where(diff != 0)[0]

                if len(edges) >= 15:
                    durations_us = np.diff(edges) * us_per_sample
                    levels = [high[edges[i] + 1] for i in range(len(edges) - 1)]

                    # Filter valid pulses (> 400 us)
                    valid_high = [durations_us[i] for i in range(len(levels)) if levels[i] == 1 and durations_us[i] >= 400]
                    valid_low  = [durations_us[i] for i in range(len(levels)) if levels[i] == 0 and durations_us[i] >= 400]

                    # Check for Euroster sync pause (6.5 - 13 ms LOW)
                    has_sync = any(6500 <= d <= 14000 for d in valid_low)

                    # Classify bits
                    bits = []
                    for dur in valid_high:
                        if 600 <= dur <= 1450: bits.append(0)
                        elif 1550 <= dur <= 2600: bits.append(1)

                    if len(bits) >= 20:
                        t_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        bit_str = "".join(str(b) for b in bits[:24])

                        id_hex = 0
                        for b in bits[:20]: id_hex = (id_hex << 1) | b

                        cmd_val = 0
                        if len(bits) >= 24:
                            for b in bits[20:24]: cmd_val = (cmd_val << 1) | b

                        cmd_name = "NECUNOSCUT"
                        if cmd_val == 0b1000: cmd_name = "HEAT ON (Pornit)"
                        elif cmd_val == 0b0100: cmd_name = "HEAT OFF (Oprit)"

                        log(f"\n >>> [SDR RF 433.92 MHz @ {t_str}] CADRU DECODIFICAT! <<<")
                        log(f"     ID: 0x{id_hex:05X} | CMD: 0b{cmd_val:04b} ({cmd_name})")
                        log(f"     Biți: {bit_str} | Vârf RF: {peak:.2f} | Sync Pause: {'DA' if has_sync else 'NU'}\n")

            time.sleep(0.005)
    finally:
        try:
            sdr.close()
        except Exception:
            pass
        log("[SDR] Închis.")


# =====================================================================
# Main
# =====================================================================
def main(duration_sec=360):
    log("=" * 75)
    log(" EUROSTER 2006 TX - MONITOR DUAL COMPLET (PPK2 + RTL-SDR)")
    log("   PPK2: D0(Reset), D1(ON), D2(OFF), D3(TX DATA)")
    log("   RTL-SDR: 433.92 MHz RF Over-The-Air")
    log(f"   Durată: {duration_sec} secunde")
    log("=" * 75)

    stop_event = threading.Event()
    t1 = threading.Thread(target=ppk2_thread, args=("COM13", stop_event), daemon=True)
    t2 = threading.Thread(target=sdr_thread, args=(433.92e6, 1.024e6, stop_event), daemon=True)

    t1.start()
    t2.start()

    try:
        t_start = time.time()
        while time.time() - t_start < duration_sec:
            time.sleep(0.5)
    except KeyboardInterrupt:
        log("\nOprire manuală...")
    finally:
        stop_event.set()
        time.sleep(1.0)
        log("Sesiune încheiată.")


if __name__ == "__main__":
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 360
    main(dur)
