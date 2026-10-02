# Euroster 2006 TX Wi-Fi Gateway & RF Sniffer (ESP32-C3)

Gateway inteligent Wi-Fi și analizor logic OOK la 433.92 MHz bazat pe **ESP32-C3** pentru emularea și clonarea termostatelor ambientale **Euroster 2006 TX** (fără a modifica receptorul de pe cazan / centrală).

Permite controlul centralei termice prin interfață web responsivă (Dark Mode), integrare în Home Assistant prin REST API și sniffing de pachete în timp real direct de pe pinul de transmisie al termostatului original.

---

## 🌟 Caracteristici

* **Emulare 100% Identică a Termostatului**: Controlează direct receptorul Euroster fără a modifica centrala.
* **Analizor Logic & Sniffer Live (Pin 5)**: Intrare High-Z (>10 MΩ) pe GPIO 5 pentru citirea impulsurilor direct din PIC-ul termostatului original fără detunarea oscilatorului RF.
* **Auto-calibrare Timpi Radio**: Măsoară automat viteza ceasului termostatului tău (~1064 µs pentru Bit 0 și ~2085 µs pentru Bit 1).
* **Interfață Web Integrată (Dark Mode)**:
  * Control ON / OFF / Keepalive manual.
  * Istoric al ultimelor 10 pachete capturate.
  * Analizor detaliat al duratelor fiecărui bit (HIGH, LOW, Perioadă).
  * Retransmitere (Replay) și tester RF manual cu număr configurabil de repetiții.
* **Keepalive Automat**: Transmisie periodică la fiecare 45 de secunde pentru a preveni decuplarea de siguranță a receptorului Euroster (~7 minute).
* **Protecție Anti-Jitter Wi-Fi**: Secțiuni critice hardware pentru precizie la nivel de microsecundă în generarea impulsurilor OOK.
* **Compatibil Home Assistant**: Controlabil complet prin REST API (`/api/on`, `/api/off`, `/api/status`).

---

## 📡 Specificații Protocol Radio Euroster 2006 TX

Protocolul utilizează modulație OOK (On-Off Keying) pe 433.92 MHz:

| Parametru | Valoare Măsurată | Descriere |
| :--- | :--- | :--- |
| **Frecvență** | 433.92 MHz | Bandă ISM |
| **Bit 0** | ~1064 µs HIGH, ~1064 µs LOW | Impuls scurt |
| **Bit 1** | ~2085 µs HIGH, ~2085 µs LOW | Impuls lung |
| **Pauză Sincronizare (Sync)** | ~9000 µs LOW | Pauză AGC între cadre |
| **Lungime Cadru** | **22 biți** | 12 biți Adresă + 4 biți CMD + 5 biți Checksum + 1 bit Stop |

### Structura Cadrului (22 Biți)

```text
[   12 biți Adresă   ] [ 4 biți CMD ] [ 5 biți Suffix ] [ 1b Stop ]
 1 1 1 0 0 1 1 0 0 0 0 0    0 0 0 1       1 0 0 1 1          1
       (0xE60)             (HEAT ON)      (Checksum)       (Stop)
```

* **Adresă / House Code (12 biți)**: Identificator unic al perechii termostat-receptor (ex: `111001100000` / `0xE60`).
* **Comandă (4 biți)**:
  * `0001` (sau `1000`) = **HEAT ON** (Pornește încălzirea / anclanșează releul)
  * `0100` = **HEAT OFF** (Oprește încălzirea / declanșează releul)
* **Suffix / Checksum (5 biți)**: `10011`
* **Stop Pulse (1 bit)**: `1`

---

## 🔌 Conexiuni Hardware (Seeed XIAO ESP32-C3)

| Pin ESP32-C3 | GPIO | Funcție | Conexiune Hardware |
| :--- | :--- | :--- | :--- |
| **D1** | GPIO 3 | Ieșire RF TX | Pinul DATA al modulului emițător 433.92 MHz (FS1000A) |
| **D3** | GPIO 5 | Intrare Sniffer | Fir conectat la pinul DATA/TX al termostatului (printr-o rezistență serie de 1kΩ–10kΩ) |
| **5V / VBUS** | - | Alimentare | 5V alimentare emițător RF |
| **GND** | - | Masă Comună | GND ESP32 legat la GND modul RF și la **GND-ul bateriilor termostatului** |

> [!TIP]
> **De ce este recomandată o rezistență serie de 1kΩ–10kΩ pe pinul de sniffer?**  
> La frecvența de comutație digitală (~1 kHz), rezistența nu modifică timpii impulsurilor. În schimb, la frecvența RF de 433.92 MHz, rezistența izolează capacitatea parazită a firului, prevenind dezacordarea oscilatorului SAW/tranzistor din termostat.

---

## 🌐 REST API

| Metodă | Endpoint | Descriere |
| :--- | :--- | :--- |
| `GET` | `/` | Interfața Web Dark Mode responsivă |
| `GET` | `/api/status` | Returnează starea curentă, datele sniffer, uptime, RSSI |
| `GET` | `/api/history` | Returnează istoricul JSON al pachetelor capturate și duratele fiecărui puls |
| `POST` | `/api/on` | Transmite comanda HEAT ON către centrală |
| `POST` | `/api/off` | Transmite comanda HEAT OFF către centrală |
| `POST` | `/api/sync` | Trimite un puls manual de sincronizare (keepalive) |
| `POST` | `/api/replay?bits=...` | Retransmite un șir arbitrar de biți pe 433.92 MHz |
| `POST` | `/api/clear_history` | Golește istoricul de pachete capturate |

---

## 🏠 Integrare Home Assistant

Adaugă în `configuration.yaml`:

```yaml
switch:
  - platform: template
    switches:
      centrala_termica:
        friendly_name: "Centrală Termică (Euroster 2006)"
        value_template: "{{ is_state('sensor.euroster_status', 'on') }}"
        turn_on:
          action: rest_command.euroster_on
        turn_off:
          action: rest_command.euroster_off

rest_command:
  euroster_on:
    url: "http://192.168.1.14/api/on"
    method: post
  euroster_off:
    url: "http://192.168.1.14/api/off"
    method: post

sensor:
  - platform: rest
    name: "Euroster Status"
    resource: "http://192.168.1.14/api/status"
    value_template: "{{ 'on' if value_json.state else 'off' }}"
    scan_interval: 10
```

---

## 🛠️ Compilare și Flash (PlatformIO)

Proiectul este configurat pe **Arduino Core 3.1.1 / ESP-IDF 5.3** (`pioarduino`) pentru a elimina complet bug-ul `WIFI_REASON_AUTH_EXPIRE` specific ESP32-C3 pe routere WPA2/WPA3.

1. Clonează repository-ul:
   ```bash
   git clone https://github.com/nor24o/Euroster-Termostat.git
   cd Euroster-Termostat
   ```

2. Configurează credențialele Wi-Fi:
   * Copiază șablonul în fișierul privat `include/secrets.h`:
     ```bash
     cp include/secrets.example.h include/secrets.h
     ```
   * Completează `WIFI_SSID` și `WIFI_PASS` cu datele rețelei tale în `include/secrets.h`.

3. Compilează și încarcă pe placă:
   ```bash
   pio run -t upload --upload-port COM32
   ```

4. Deschide `http://euroster.local` sau IP-ul alocat în browser!
