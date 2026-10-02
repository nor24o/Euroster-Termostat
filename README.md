# Euroster 2006 TX Wi-Fi Gateway & RF Controller (ESP32-C3)

Gateway inteligent Wi-Fi și transmițător OOK la 433.92 MHz bazat pe **ESP32-C3** pentru emularea și controlul complet al receptoarelor de cazan / centrală **Euroster 2006 RX** (fără a modifica receptorul sau instalația de încălzire).

Include stocare persistentă în Flash (NVS), interfață web responsivă (Dark Mode), integrare Home Assistant prin REST API și un **Scanner Automat (Brute-Force)** capabil să deducă și să găsească codul oricărui receptor Euroster în câteva minute chiar dacă nu ai termostatul original.

---

## 🌟 Caracteristici

* **Emulare 100% Identică a Termostatului**: Emite cadrele pe 22 de biți pe care le așteaptă receptorul Euroster 2006.
* **Memorie Persistentă Flash (NVS)**: Salvează House Code-ul în memoria non-volatilă a ESP32, rezistând la reporniri sau căderi de tensiune.
* **Scanner Automat House Code (Brute-force 12 biți)**:
  * Parcurge automat toate cele 4096 de coduri posibile (`0x000` – `0xFFF`) la viteză mare (~5.5 coduri/secundă).
  * Oprești scanarea când auzi releul centralei comutând și testezi codurile recente pentru a salva codul definitiv.
* **Reglaj Fin și Testare Pas cu Pas**: Butoane de `-1`, `+1` și testare instantă direct din Web UI.
* **Interfață Web Integrată (Dark Mode)**:
  * Control manual ON / OFF / Keepalive.
  * Configurare și afișare cod activ în formatele Hex (`0xE60`), Zecimal (`3680`) și Binar (`111001100000`).
* **Keepalive Automat**: Transmisie periodică la fiecare 45 de secunde pentru a preveni decuplarea de siguranță a receptorului Euroster (~7 minute).
* **Protecție Anti-Jitter Wi-Fi**: Secțiuni critice hardware pentru precizie la nivel de microsecundă în generarea impulsurilor OOK.
* **Compatibil Home Assistant**: Controlabil complet prin REST API (`/api/on`, `/api/off`, `/api/status`, `/api/set_house_code`).

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

* **Adresă / House Code (12 biți)**: Identificator unic al perechii termostat-receptor (ex: `111001100000` / `0xE60` / `3680`).
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
| **5V / VBUS** | - | Alimentare | 5V alimentare emițător RF |
| **GND** | - | Masă Comună | GND ESP32 legat la GND-ul modulului RF |

---

## 🔍 Cum găsești House Code-ul dacă nu ai termostatul original

Există 3 moduri de a stabili comunicarea cu receptorul Euroster de la cazan:

1. **Scanner Automat Brute-Force (din Web UI)**:
   * Deschide interfața la `http://euroster.local/`.
   * În secțiunea **Scanner Automat**, apasă **`▶️ Pornește Scanare Automată`**.
   * ESP32 va parcurge pe rând cele 4096 de coduri trimițând impulsuri de pornire.
   * Când auzi releul centralei comutând (sau vezi LED-ul verde aprins pe receptor), apasă **`🛑 AM AUZIT RELEUL! (STOP)`**.
   * Interfața îți afișează ultimele coduri testate; apeși pe ele pentru a confirma exact codul și apoi **`💾 Salvează în Flash`**.
2. **Citirea etichetei de pe receptor**:
   * Pe spatele receptorului RX (sau lângă conectori) există o etichetă cu numărul de serie / codul de fabrică corespunzător celor 12 biți.
3. **Modul de învățare (Pairing) al receptorului**:
   * Dacă receptorul Euroster are buton de pairing (adesea marcat cu **E**), apăsarea lui de 3 ori activează modul de învățare (LED albastru). Emite orice cod din ESP32, iar receptorul se va împerechea cu el.

---

## 🌐 REST API

| Metodă | Endpoint | Descriere |
| :--- | :--- | :--- |
| `GET` | `/` | Interfața Web Dark Mode responsivă |
| `GET` | `/api/status` | Starea curentă a centralei, House Code activ, RSSI, Uptime |
| `POST` | `/api/on` | Transmite comanda HEAT ON către centrală |
| `POST` | `/api/off` | Transmite comanda HEAT OFF către centrală |
| `POST` | `/api/sync` | Trimite un puls manual de sincronizare (keepalive) |
| `POST` | `/api/set_house_code?code=0xE60` | Salvează un nou House Code în memoria NVS Flash |
| `POST` | `/api/test_code?code=0xE60&action=on` | Trimite un puls de test cu un cod specificat |
| `POST` | `/api/scan_start` | Pornește scanarea automată brute-force |
| `POST` | `/api/scan_stop` | Oprește scanarea și returnează ultimele coduri parcurse |
| `GET` | `/api/scan_status` | Returnează progresul curent al scanerului |

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
