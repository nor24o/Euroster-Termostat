"""
Euroster 2006 TX - Full Microcontroller & RF Modulator Logic Analyzer (PPK2)
Monitors:
  - D0: RESET (MCLR, active-low)
  - D1: ON trigger (MCU -> PIC)
  - D2: OFF trigger (MCU -> PIC)
  - D3: TX DATA (PIC -> RF 433.92 MHz module)

Sample rate: 100 kSps (10 µs resolution)
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
    import serial
    from ppk2_api.ppk2_api import PPK2_API, PPK2_Command
except ImportError:
    print("Error: ppk2-api or pyserial not installed.")
    sys.exit(1)


def flush_ppk2(port="COM13"):
    try:
        s = serial.Serial(port, 9600, timeout=0.2)
        s.write(bytes([PPK2_Command.AVERAGE_STOP]))
        time.sleep(0.1)
        s.reset_input_buffer()
        s.close()
    except Exception:
        pass


def init_ppk2(port="COM13"):
    flush_ppk2(port)
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
    return ppk2


def analyze_transaction(burst, burst_id, output_dir="captures"):
    """
    Analyzes the captured transitions across D0..D3.
    Decodes D3 pulse durations into Euroster bits.
    """
    t_start = burst[0]["time"]
    t_end = burst[-1]["time"]
    total_ms = (t_end - t_start) * 1000

    d0_events = [e for e in burst if "D0" in e["label"]]
    d1_events = [e for e in burst if "D1" in e["label"]]
    d2_events = [e for e in burst if "D2" in e["label"]]
    d3_events = [e for e in burst if "D3" in e["label"]]

    cmd_source = "NECUNOSCUT"
    if len(d1_events) > 0 and len(d2_events) == 0:
        cmd_source = "HEAT ON (Pornire Căldură)"
    elif len(d2_events) > 0 and len(d1_events) == 0:
        cmd_source = "HEAT OFF (Oprire Căldură)"
    elif len(d1_events) > 0 and len(d2_events) > 0:
        cmd_source = "AMBELE LINII D1 & D2"

    print("\n" + "=" * 70)
    print(f" >>> TRANZACȚIE TERMOSTAT DETECTATĂ (Secvență #{burst_id}) <<<")
    print(f"   Comandă MCU: {cmd_source}")
    print(f"   Tranziții: {len(burst)} (D0/Reset: {len(d0_events)}, D1/ON: {len(d1_events)}, D2/OFF: {len(d2_events)}, D3/DATA: {len(d3_events)})")
    print(f"   Durată totală: {total_ms:.2f} ms")

    # Decode D3 if there are pulses on D3
    if len(d3_events) >= 10:
        d3_pulses = []  # (level, duration_us)
        for i in range(len(d3_events) - 1):
            lvl = d3_events[i]["d3"]
            dur_us = int((d3_events[i+1]["time"] - d3_events[i]["time"]) * 1_000_000)
            d3_pulses.append((lvl, dur_us))

        print(f"\n   [D3 TX DATA] Total impulsuri modulate captate: {len(d3_pulses)}")

        # Find sync gaps (LOW level > 6000 us)
        sync_gaps = [i for i, (lvl, dur) in enumerate(d3_pulses) if lvl == 0 and 6500 <= dur <= 14000]
        print(f"   [D3 TX DATA] Pauze Sync (~9 ms) găsite: {len(sync_gaps)}")

        # Decode frames
        if sync_gaps:
            for s_num, s_idx in enumerate(sync_gaps[:3]):
                # Take pulses after this sync
                sub_pulses = d3_pulses[s_idx + 1: s_idx + 60]
                high_durations = [dur for lvl, dur in sub_pulses if lvl == 1]
                bits = []
                for dur in high_durations:
                    if 600 <= dur <= 1400:
                        bits.append(0)
                    elif 1600 <= dur <= 2500:
                        bits.append(1)
                    else:
                        bits.append(-1)

                if len(bits) >= 24 and (-1 not in bits[:24]):
                    id_bits = bits[:20]
                    cmd_bits = bits[20:24]
                    id_hex = 0
                    for b in id_bits:
                        id_hex = (id_hex << 1) | b
                    cmd_val = 0
                    for b in cmd_bits:
                        cmd_val = (cmd_val << 1) | b

                    bit_str = "".join(str(b) for b in bits[:24])
                    print(f"   >>> CADRU D3 #{s_num+1}: ID=0x{id_hex:05X} | CMD=0b{cmd_val:04b} | Biți: {bit_str}")
        else:
            # If no sync gap was within the slice, inspect raw high pulses
            high_durs = [dur for lvl, dur in d3_pulses if lvl == 1]
            bits = [0 if 600 <= d <= 1400 else (1 if 1600 <= d <= 2500 else -1) for d in high_durs]
            if len(bits) >= 24 and (-1 not in bits[:24]):
                id_hex = sum(b << (19 - i) for i, b in enumerate(bits[:20]))
                cmd_val = sum(b << (3 - i) for i, b in enumerate(bits[20:24]))
                print(f"   >>> CADRU D3: ID=0x{id_hex:05X} | CMD=0b{cmd_val:04b} | Biți: {''.join(str(b) for b in bits[:24])}")

    print("=" * 70 + "\n")

    # Save to JSON
    filename = os.path.join(output_dir, f"ppk2_tx_capture_{burst_id:03d}_{int(time.time())}.json")
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump({
                "burst_id": burst_id,
                "cmd_source": cmd_source,
                "duration_ms": total_ms,
                "events": burst
            }, f, indent=2)
        print(f"   [Salvat în: {filename}]")
    except Exception as e:
        print(f"   [Eroare salvare]: {e}")


def monitor(duration_sec=180, port="COM13"):
    os.makedirs("captures", exist_ok=True)
    print("=" * 75)
    print(" EUROSTER 2006 TX - ANALIZOR LOGIC DIGITAL PPK2 (D0, D1, D2, D3)")
    print("   Linii monitorizate:")
    print("     D0: RESET (MCLR, nivel normal 1)")
    print("     D1: Comandă ON de la MCU (nivel normal 0)")
    print("     D2: Comandă OFF de la MCU (nivel normal 0)")
    print("     D3: DATA ieșire PIC către modulul TX (nivel normal 0)")
    print(f" Port: {port} | Rezoluție: 10 µs | Durată sesiune: {duration_sec} secunde")
    print("=" * 75)

    try:
        ppk2 = init_ppk2(port)
    except Exception as e:
        print(f"[EROARE] Nu s-a putut inițializa PPK2: {e}")
        return

    ppk2.start_measuring()
    print("\n>>> ANALIZORUL ESTE ACTIV! APASĂ PE BUTOANELE TERMOSTATULUI ACUM! <<<\n")

    # Read initial state
    time.sleep(0.05)
    init_data = ppk2.get_data()
    last_state = (1, 0, 0, 0)
    if init_data:
        _, raw_init = ppk2.get_samples(init_data)
        if raw_init:
            b = raw_init[-1]
            last_state = ((b >> 0) & 1, (b >> 1) & 1, (b >> 2) & 1, (b >> 3) & 1)
            print(f"[STARE INIȚIALĂ] D0(Reset)={last_state[0]}, D1(ON)={last_state[1]}, D2(OFF)={last_state[2]}, D3(TX_DATA)={last_state[3]}\n")

    last_change_time = time.time()
    t_start = time.time()
    current_burst = []
    burst_id = 0

    try:
        while time.time() - t_start < duration_sec:
            data = ppk2.get_data()
            if not data:
                time.sleep(0.005)
                if current_burst and (time.time() - last_change_time > 0.25):
                    burst_id += 1
                    analyze_transaction(current_burst, burst_id)
                    current_burst = []
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

                    label_parts = []
                    if curr_state[0] != last_state[0]:
                        label_parts.append(f"D0(RESET)={'HIGH' if d0 else 'LOW'}")
                    if curr_state[1] != last_state[1]:
                        label_parts.append(f"D1(ON)={'HIGH' if d1 else 'LOW'}")
                    if curr_state[2] != last_state[2]:
                        label_parts.append(f"D2(OFF)={'HIGH' if d2 else 'LOW'}")
                    if curr_state[3] != last_state[3]:
                        label_parts.append(f"D3(DATA)={'HIGH' if d3 else 'LOW'}")
                    label = ", ".join(label_parts)

                    event = {
                        "time": now,
                        "time_str": t_str,
                        "dt_us": dt_us,
                        "d0": d0,
                        "d1": d1,
                        "d2": d2,
                        "d3": d3,
                        "label": label
                    }
                    current_burst.append(event)

                    # Only print MCU control transitions directly to avoid flooding with D3 100 pulses
                    if curr_state[0] != last_state[0] or curr_state[1] != last_state[1] or curr_state[2] != last_state[2]:
                        print(f"[{t_str}] D0={d0} D1={d1} D2={d2} D3={d3} | dt={dt_us:>7} µs | {label}")

                    last_state = curr_state
                    last_change_time = now

    except KeyboardInterrupt:
        print("\nOprit de utilizator.")
    finally:
        if current_burst:
            burst_id += 1
            analyze_transaction(current_burst, burst_id)
        try:
            ppk2.stop_measuring()
            ppk2.ser.close()
        except Exception:
            pass
        print(f"\n[FINALIZAT] Sesiune încheiată. Tranzacții captate: {burst_id}")


if __name__ == "__main__":
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    monitor(dur)
