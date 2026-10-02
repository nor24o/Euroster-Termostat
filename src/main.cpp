#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <esp_wifi.h>

// ======================= CONFIGURARE HARDWARE =======================
#define RF_PIN              3      // Pinul DATA conectat la modulul emițător 433.92MHz (D1 pe XIAO ESP32-C3)
#define HEARTBEAT_INTERVAL  45000  // Keepalive la fiecare 45 secunde (Receptorul taie la ~7 min)
#define ENABLE_SNIFFER      0      // 0 = Dezactivat complet (Mod Producție), 1 = Activ (Diagnostic Pin 5)

#if ENABLE_SNIFFER
#define SNIFFER_PIN         5      // Pinul de citire (D3 / GPIO 5 pe XIAO ESP32-C3)
#endif

// ======================= CONFIGURARE WI-FI (STA) =====================
// Credentialele se configureaza in include/secrets.h (vezi secrets.example.h)
#include "secrets.h"

// ======================= PROTOCOL RADIO 433.92 MHz ===================
// Protocol Euroster 2006 TX (OOK / Pulse Distance) - Calibrat pe termostatul real
const uint16_t T_SHORT = 1064;     // 1064 µs (Bit 0)
const uint16_t T_LONG  = 2085;     // 2085 µs (Bit 1)
const uint16_t T_SYNC  = 9000;     // 9000 µs (Pauză sincronizare AGC)

// Șabloane 22 biți decodificate exact din termostatul fizic
// Format: [12b Adresă: 111001100000 (0xE60)] [4b CMD] [5b Suffix: 10011] [1b Stop: 1]
const char* FRAME_ON       = "1110011000000001100111"; // Comandă HEAT ON (CMD: 0001)
const char* FRAME_OFF      = "1110011000000100100111"; // Comandă HEAT OFF (CMD: 0100)
const char* HOUSE_CODE_STR = "0xE60 (111001100000)";

// ======================= VARIABILE DE STARE GATEWAY ==================
WebServer server(80);
bool heatState = false;
unsigned long lastHeartbeat = 0;
uint32_t txCount = 0;
String lastTxStatus = "Nicio comandă transmisă încă";
unsigned long lastWifiRetry = 0;

// Mutex pentru protecția sincronizării impulsurilor radio
portMUX_TYPE txMux = portMUX_INITIALIZER_UNLOCKED;

// ======================= FUNCȚII EMISIE RADIO =======================
inline void sendPulse(bool isLong) {
  uint16_t duration = isLong ? T_LONG : T_SHORT;
  portENTER_CRITICAL(&txMux);
  digitalWrite(RF_PIN, HIGH);
  delayMicroseconds(duration);
  digitalWrite(RF_PIN, LOW);
  delayMicroseconds(duration);
  portEXIT_CRITICAL(&txMux);
}

void transmitFrame(const char* bitStr, uint8_t repeats = 6) {
  if (!bitStr || strlen(bitStr) == 0) return;

  for (uint8_t r = 0; r < repeats; r++) {
    for (size_t i = 0; bitStr[i] != '\0'; i++) {
      sendPulse(bitStr[i] == '1');
    }
    digitalWrite(RF_PIN, LOW);
    delayMicroseconds(T_SYNC);
  }

  txCount++;
  Serial.printf("[RF 433.92] Transmis cadru: %s (x%d)\n", bitStr, repeats);
}

void transmitRF(bool turnOn, uint8_t repeats = 6) {
  const char* frame = turnOn ? FRAME_ON : FRAME_OFF;
  transmitFrame(frame, repeats);

  heatState = turnOn;
  lastHeartbeat = millis();
  lastTxStatus = turnOn ? "HEAT ON (Pornit)" : "HEAT OFF (Oprit)";

  Serial.printf("[RF 433.92] Comandă executată: %s\n", lastTxStatus.c_str());
}

// ======================= INTERFAȚĂ WEB RESPONSIVE (DARK MODE) ========
const char INDEX_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="ro">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Termostat Euroster 2006 Gateway</title>
  <style>
    :root {
      --bg: #0b1120;
      --card: #1e293b;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --green: #22c55e;
      --green-glow: rgba(34, 197, 94, 0.25);
      --red: #ef4444;
      --red-glow: rgba(239, 68, 68, 0.25);
      --border: #334155;
      --primary: #38bdf8;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; -webkit-tap-highlight-color: transparent; }
    body { background-color: var(--bg); color: var(--text); display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 16px; }
    .card { background-color: var(--card); border: 1px solid var(--border); border-radius: 20px; width: 100%; max-width: 440px; padding: 26px; box-shadow: 0 15px 35px rgba(0,0,0,0.6); text-align: center; }
    .header { margin-bottom: 20px; }
    h1 { font-size: 1.35rem; font-weight: 700; letter-spacing: -0.5px; }
    .subtitle { font-size: 0.85rem; color: var(--text-muted); margin-top: 4px; }
    
    .status-badge { display: inline-flex; align-items: center; justify-content: center; gap: 10px; padding: 11px 24px; border-radius: 9999px; font-weight: 700; font-size: 1.05rem; margin-bottom: 24px; transition: all 0.3s ease; }
    .status-on { background-color: var(--green-glow); color: var(--green); border: 1.5px solid var(--green); box-shadow: 0 0 15px var(--green-glow); }
    .status-off { background-color: var(--red-glow); color: var(--red); border: 1.5px solid var(--red); box-shadow: 0 0 15px var(--red-glow); }
    .dot { width: 12px; height: 12px; border-radius: 50%; }
    .dot-on { background-color: var(--green); box-shadow: 0 0 10px var(--green); }
    .dot-off { background-color: var(--red); }
    
    .btn-group { display: flex; flex-direction: column; gap: 14px; margin-bottom: 24px; }
    button { border: none; border-radius: 14px; padding: 18px; font-size: 1.05rem; font-weight: 700; cursor: pointer; transition: all 0.15s ease; display: flex; justify-content: center; align-items: center; gap: 10px; width: 100%; }
    button:active { transform: scale(0.97); }
    .btn-on { background: linear-gradient(135deg, #22c55e, #16a34a); color: #022c22; box-shadow: 0 4px 15px rgba(34, 197, 94, 0.35); }
    .btn-off { background: linear-gradient(135deg, #ef4444, #dc2626); color: #ffffff; box-shadow: 0 4px 15px rgba(239, 68, 68, 0.35); }
    .btn-sync { background-color: #0f172a; border: 1px solid var(--border); color: var(--text-muted); padding: 12px; font-size: 0.85rem; font-weight: 600; border-radius: 10px; }
    .btn-sync:hover { color: var(--text); border-color: var(--primary); }

    .meta-box { border-top: 1px solid var(--border); padding-top: 18px; text-align: left; font-size: 0.82rem; color: var(--text-muted); display: flex; flex-direction: column; gap: 9px; }
    .meta-row { display: flex; justify-content: space-between; align-items: center; }
    .meta-val { color: var(--text); font-weight: 600; font-family: monospace; font-size: 0.88rem; }
    
    .toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #1e293b; color: #fff; padding: 10px 20px; border-radius: 30px; font-size: 0.85rem; border: 1px solid var(--border); opacity: 0; pointer-events: none; transition: opacity 0.3s; z-index: 100; box-shadow: 0 5px 15px rgba(0,0,0,0.5); }
    .toast.show { opacity: 1; }
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>Euroster 2006 TX</h1>
      <div class="subtitle">Wi-Fi RF Gateway (ESP32-C3)</div>
    </div>

    <div id="badge" class="status-badge status-off">
      <div id="dot" class="dot dot-off"></div>
      <span id="state-text">OPRIT (STANDBY)</span>
    </div>

    <div class="btn-group">
      <button class="btn-on" onclick="setThermostat('on')">
        🔥 PORNEȘTE CĂLDURA (ON)
      </button>
      <button class="btn-off" onclick="setThermostat('off')">
        ❄️ OPREȘTE CĂLDURA (OFF)
      </button>
      <button class="btn-sync" onclick="setThermostat('sync')">
        🔄 Trimite Puls Keepalive Acum
      </button>
    </div>

    <div class="meta-box">
      <div class="meta-row"><span>Ultima comandă RF:</span><span id="last-tx" class="meta-val">-</span></div>
      <div class="meta-row"><span>Următorul Keepalive:</span><span id="next-ka" class="meta-val">-</span></div>
      <div class="meta-row"><span>Total transmisii RF:</span><span id="tx-count" class="meta-val">0</span></div>
      <div class="meta-row"><span>House Code (ID):</span><span class="meta-val">0xE60</span></div>
      <div class="meta-row"><span>Semnal Wi-Fi (RSSI):</span><span id="wifi-rssi" class="meta-val">-</span></div>
      <div class="meta-row"><span>Timp funcționare:</span><span id="uptime" class="meta-val">-</span></div>
    </div>
  </div>

  <div id="toast" class="toast">Comandă trimisă!</div>

  <script>
    function showToast(msg) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.className = "toast show";
      setTimeout(() => { t.className = "toast"; }, 2000);
    }

    function formatTime(sec) {
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = sec % 60;
      if (h > 0) return `${h}h ${m}m ${s}s`;
      if (m > 0) return `${m}m ${s}s`;
      return `${s}s`;
    }

    function updateUI(data) {
      const badge = document.getElementById('badge');
      const dot = document.getElementById('dot');
      const stateText = document.getElementById('state-text');
      
      if (data.state) {
        badge.className = "status-badge status-on";
        dot.className = "dot dot-on";
        stateText.innerText = "CĂLDURĂ PORNITĂ";
      } else {
        badge.className = "status-badge status-off";
        dot.className = "dot dot-off";
        stateText.innerText = "OPRIT (STANDBY)";
      }

      document.getElementById('last-tx').innerText = data.lastTx;
      document.getElementById('tx-count').innerText = data.count;
      document.getElementById('next-ka').innerText = data.nextKa + ' secunde';
      document.getElementById('wifi-rssi').innerText = data.rssi + ' dBm';
      document.getElementById('uptime').innerText = formatTime(data.uptime);
    }

    async function setThermostat(action) {
      try {
        const res = await fetch('/api/' + action, { method: 'POST' });
        const data = await res.json();
        updateUI(data);
        showToast(action === 'on' ? '🔥 Căldură pornită!' : (action === 'off' ? '❄️ Căldură oprită!' : '🔄 Puls trimis!'));
      } catch (err) {
        showToast('Eroare conexiune!');
      }
    }

    async function pollStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        updateUI(data);
      } catch (err) {}
    }

    setInterval(pollStatus, 3000);
    pollStatus();
  </script>
</body>
</html>
)rawliteral";

// ======================= HANDLERE HTTP & REST API ===================
void handleRoot() {
  server.send_P(200, "text/html", INDEX_HTML);
}

void sendJsonResponse() {
  unsigned long elapsed = millis() - lastHeartbeat;
  int remainingKa = (HEARTBEAT_INTERVAL > elapsed) ? (HEARTBEAT_INTERVAL - elapsed) / 1000 : 0;

  String json = "{";
  json += "\"state\":" + String(heatState ? "true" : "false") + ",";
  json += "\"lastTx\":\"" + lastTxStatus + "\",";
  json += "\"count\":" + String(txCount) + ",";
  json += "\"nextKa\":" + String(remainingKa) + ",";
  json += "\"rssi\":" + String(WiFi.RSSI()) + ",";
  json += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
  json += "\"uptime\":" + String(millis() / 1000) + ",";
  json += "\"houseCode\":\"" + String(HOUSE_CODE_STR) + "\"";
  json += "}";

  server.send(200, "application/json", json);
}

void handleApiOn() {
  transmitRF(true, 6);
  sendJsonResponse();
}

void handleApiOff() {
  transmitRF(false, 6);
  sendJsonResponse();
}

void handleApiSync() {
  transmitRF(heatState, 4);
  sendJsonResponse();
}

// ======================= SETUP ȘI LOOP ==============================
void setup() {
  Serial.begin(115200);
  
  // 1. Configurare Pin TX RF 433 MHz (Ieșire)
  pinMode(RF_PIN, OUTPUT);
  digitalWrite(RF_PIN, LOW);

#if ENABLE_SNIFFER
  pinMode(SNIFFER_PIN, INPUT);
  gpio_pullup_dis((gpio_num_t)SNIFFER_PIN);
  gpio_pulldown_dis((gpio_num_t)SNIFFER_PIN);
#endif

  delay(500);
  Serial.println("\n=============================================");
  Serial.println("  Euroster 2006 TX Wi-Fi Gateway (ESP32-C3)  ");
  Serial.println("  Mod Producție - Emulare 22 Biți (0xE60)   ");
  Serial.println("=============================================");

  // 2. Initializare mod Station curat cu auto-reconnect
  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  WiFi.mode(WIFI_STA);

  // 3. Fortare 802.11 b/g/n (compatibilitate maxima)
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);

  // 4. Conectare Wi-Fi
  Serial.printf("[WiFi] Conectare la '%s'...", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  // 5. Setare TX Power la 8.5 dBm
  WiFi.setTxPower(WIFI_POWER_8_5dBm);

  uint8_t retries = 0;
  while (WiFi.status() != WL_CONNECTED && retries < 40) {
    delay(350);
    Serial.print(".");
    retries++;
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("[WiFi] CONECTAT CU SUCCES!");
    Serial.printf("[WiFi] IP local: http://%s/\n", WiFi.localIP().toString().c_str());

    if (MDNS.begin(MDNS_HOST)) {
      Serial.printf("[mDNS] Accesibil la: http://%s.local\n", MDNS_HOST);
      MDNS.addService("http", "tcp", 80);
    }

    server.on("/", HTTP_GET, handleRoot);
    server.on("/api/status", HTTP_GET, sendJsonResponse);
    server.on("/api/on", HTTP_POST, handleApiOn);
    server.on("/api/off", HTTP_POST, handleApiOff);
    server.on("/api/sync", HTTP_POST, handleApiSync);

    server.begin();
    Serial.println("[HTTP] Serverul web a pornit.");
  } else {
    Serial.printf("[WiFi] Esec conectare initiala. Cod status: %d\n", WiFi.status());
  }

  lastHeartbeat = millis();
}

void loop() {
  if (WiFi.status() == WL_CONNECTED) {
    server.handleClient();
  } else {
    if (millis() - lastWifiRetry >= 10000) {
      lastWifiRetry = millis();
      Serial.println("[WiFi] Reincercare conectare...");
      WiFi.reconnect();
    }
  }

  // Puls periodic keepalive pentru receptorul Euroster (la fiecare 45 secunde)
  if (millis() - lastHeartbeat >= HEARTBEAT_INTERVAL) {
    Serial.println("[RF 433.92] Trimitere puls Keepalive automat...");
    transmitRF(heatState, 3);
  }
}