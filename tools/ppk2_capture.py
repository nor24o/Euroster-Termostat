"""
Euroster 2006 TX - PPK2 Digital Logic Analyzer & Event Decoder
Monitors:
  - D0: RESET pin (from main MCU to PIC TX sender)
  - D1: ON pin
  - D2: OFF pin

Sample rate: 100 kHz (10 microseconds per sample)
"""

import time
import sys
import os
import json
from datetime import datetime

# Ensure stdout handles UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    import serial
    from ppk2_api.ppk2_api import PPK2_API, PPK2_Command
except ImportError:
    print("Error: ppk2-api or pyserial not installed.")
    sys.exit(1)


def flush_ppk2(port="COM13"):
    """Reset communication and flush any pending bytes on the PPK2."""
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

    # Monkey patch to avoid UTF-8 decode crash when binary residue is present
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


def monitor_digital_pins(port="COM13", duration_sec=180, output_dir="captures"):
    os.makedirs(output_dir, exist_ok=True)
    print("=" * 70)
    print(" EUROSTER 2006 TX -> PIC TX LOGIC MONITOR (PPK2)")
    print("   Pin Mapping:")
    print("     D0: RESET (Pin catre microcontrolerul TX PIC)")
    print("     D1: ON    (Semnal comanda pornire caldura)")
    print("     D2: OFF   (Semnal comanda oprire caldura)")
    print(f" Port: {port} | Sample Rate: 100 kSps (10 us)")
    if duration_sec:
        print(f" Durata sesiune: {duration_sec} secunde (sau pana la oprire manuala)")
    print("=" * 70)

    try:
        ppk2 = init_ppk2(port)
    except Exception as e:
        print(f"[EROARE] Nu s-a putut initializa PPK2 pe {port}: {e}")
        return

    ppk2.start_measuring()
    print("\n>>> PPK2 ESTE ACTIV SI ASCULTA. APASA PE BUTOANELE TERMOSTATULUI! <<<\n")

    # Read initial baseline
    time.sleep(0.05)
    init_data = ppk2.get_data()
    last_state = (0, 0, 0)
    if init_data:
        _, raw_init = ppk2.get_samples(init_data)
        if raw_init:
            last_byte = raw_init[-1]
            last_state = ((last_byte >> 0) & 1, (last_byte >> 1) & 1, (last_byte >> 2) & 1)
            print(f"[STARE INITIALA] D0(Reset)={last_state[0]}, D1(ON)={last_state[1]}, D2(OFF)={last_state[2]}\n")

    last_change_time = time.time()
    t_start = time.time()
    all_events = []
    current_burst = []
    burst_start_time = None
    burst_id = 0

    try:
        while True:
            if duration_sec and (time.time() - t_start > duration_sec):
                print(f"\n[INFO] Timpul de monitorizare ({duration_sec}s) a expirat.")
                break

            data = ppk2.get_data()
            if not data:
                time.sleep(0.005)
                # Check for burst conclusion if idle > 150 ms
                if current_burst and (time.time() - last_change_time > 0.15):
                    burst_duration_ms = (last_change_time - burst_start_time) * 1000
                    burst_id += 1
                    summary = summarize_burst(current_burst, burst_id, burst_duration_ms)
                    print(summary)
                    save_burst(output_dir, burst_id, current_burst, summary)
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
                    timestamp_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]

                    label_parts = []
                    if curr_state[0] != last_state[0]:
                        label_parts.append(f"D0(RESET) -> {'HIGH' if d0 else 'LOW'}")
                    if curr_state[1] != last_state[1]:
                        label_parts.append(f"D1(ON)    -> {'HIGH' if d1 else 'LOW'}")
                    if curr_state[2] != last_state[2]:
                        label_parts.append(f"D2(OFF)   -> {'HIGH' if d2 else 'LOW'}")
                    label = ", ".join(label_parts)

                    event_entry = {
                        "time": now,
                        "time_str": timestamp_str,
                        "dt_us": dt_us,
                        "d0": d0,
                        "d1": d1,
                        "d2": d2,
                        "label": label
                    }

                    if not current_burst:
                        burst_start_time = now

                    current_burst.append(event_entry)
                    all_events.append(event_entry)

                    print(f"[{timestamp_str}] D0={d0} D1={d1} D2={d2} | dt={dt_us:>7} us | {label}")

                    last_state = curr_state
                    last_change_time = now

    except KeyboardInterrupt:
        print("\n[INFO] Monitorizare oprita de utilizator (Ctrl+C).")
    finally:
        # Flush any remaining burst
        if current_burst:
            burst_duration_ms = (last_change_time - burst_start_time) * 1000
            burst_id += 1
            summary = summarize_burst(current_burst, burst_id, burst_duration_ms)
            print(summary)
            save_burst(output_dir, burst_id, current_burst, summary)

        try:
            ppk2.stop_measuring()
            ppk2.ser.close()
        except Exception:
            pass
        print(f"\n[FINALIZAT] Total evenimente captate: {len(all_events)}, Pachete/Secvente: {burst_id}")


def summarize_burst(burst, burst_id, duration_ms):
    d0_toggles = sum(1 for e in burst if "D0" in e["label"])
    d1_toggles = sum(1 for e in burst if "D1" in e["label"])
    d2_toggles = sum(1 for e in burst if "D2" in e["label"])

    intent = "NECUNOSCUT"
    if d1_toggles > 0 and d2_toggles == 0:
        intent = ">>> COMANDA DETECTATA: HEAT ON (Pornire) <<<"
    elif d2_toggles > 0 and d1_toggles == 0:
        intent = ">>> COMANDA DETECTATA: HEAT OFF (Oprire) <<<"
    elif d1_toggles > 0 and d2_toggles > 0:
        intent = ">>> AMBELE LINII (ON & OFF) AU FOST ACTIVE <<<"
    elif d0_toggles > 0:
        intent = ">>> DOAR IMPULS PE D0 (RESET) <<<"

    lines = [
        f"\n------------------- [EVENIMENT #{burst_id} DETECTAT] -------------------",
        f"  Interpretare: {intent}",
        f"  Tranzitii: {len(burst)} (D0: {d0_toggles}, D1: {d1_toggles}, D2: {d2_toggles})",
        f"  Durata totala secventa: {duration_ms:.2f} ms",
        "------------------------------------------------------------------------\n"
    ]
    return "\n".join(lines)


def save_burst(output_dir, burst_id, burst, summary):
    filename = os.path.join(output_dir, f"burst_{burst_id:03d}_{int(time.time())}.json")
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump({
                "burst_id": burst_id,
                "summary": summary,
                "events": burst
            }, f, indent=2)
        print(f"  [Salvat in: {filename}]")
    except Exception as e:
        print(f"  [Eroare salvare]: {e}")


if __name__ == "__main__":
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    monitor_digital_pins("COM13", duration_sec=dur)
