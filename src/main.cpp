#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <esp_wifi.h>
#include <nvs_flash.h>
#include <nvs.h>

// Credentialele se configureaza in include/secrets.h (vezi secrets.example.h)
#include "secrets.h"

// ======================= CONFIGURARE HARDWARE =======================
#define RF_PIN              3      // Pinul DATA conectat la modulul emițător 433.92MHz (D1 pe XIAO ESP32-C3)
#define HEARTBEAT_INTERVAL  45000  // Keepalive la fiecare 45 secunde (Receptorul taie la ~7 min)

// ======================= PROTOCOL RADIO 433.92 MHz ===================
// Protocol Euroster 2006 TX (OOK / Pulse Distance) - Calibrat pe termostatul real
const uint16_t T_SHORT = 1064;     // 1064 µs (Bit 0)
const uint16_t T_LONG  = 2085;     // 2085 µs (Bit 1)
const uint16_t T_SYNC  = 9000;     // 9000 µs (Pauză sincronizare AGC)

// Codul de casă (House Code) de 12 biți (0x000 .. 0xFFF)
// Salvat persistent în NVS Flash (implicit: 0xE60 / 3680)
uint16_t currentHouseCode = 0x0E60;

// ======================= SCANNER / BRUTE-FORCE STATE =================
bool isScanning = false;
uint16_t scanCurrentCode = 0x0000;
uint16_t scanEndCode     = 0x0FFF;
unsigned long scanLastStepTime = 0;
const uint16_t SCAN_STEP_INTERVAL = 180; // ms per cod (~5.5 coduri/secundă)

#define RECENT_SCAN_MAX 8
uint16_t recentScanCodes[RECENT_SCAN_MAX];
uint8_t recentScanCount = 0;

// ======================= VARIABILE DE STARE GATEWAY ==================
WebServer server(80);
bool heatState = false;
unsigned long lastHeartbeat = 0;
uint32_t txCount = 0;
String lastTxStatus = "Nicio comandă transmisă încă";
unsigned long lastWifiRetry = 0;

// Mutex pentru protecția sincronizării impulsurilor radio
portMUX_TYPE txMux = portMUX_INITIALIZER_UNLOCKED;

// ======================= FUNCȚII PROTOCOL RADIO =====================
inline void sendPulse(bool isLong) {
  uint16_t duration = isLong ? T_LONG : T_SHORT;
  portENTER_CRITICAL(&txMux);
  digitalWrite(RF_PIN, HIGH);
  delayMicroseconds(duration);
  digitalWrite(RF_PIN, LOW);
  delayMicroseconds(duration);
  portEXIT_CRITICAL(&txMux);
}

// Generează șirul binar de 12 biți pentru un House Code
String to12BitBinary(uint16_t code) {
  String s = "";
  for (int8_t i = 11; i >= 0; i--) {
    s += ((code >> i) & 1) ? '1' : '0';
  }
  return s;
}

// Generează cadrul de 22 biți: [12b Adresă] [4b CMD] [5b Suffix: 10011] [1b Stop: 1]
String buildFrame(uint16_t code, bool turnOn) {
  String frame = to12BitBinary(code);
  frame += turnOn ? "0001" : "0100"; // CMD: 0001 = ON, 0100 = OFF
  frame += "10011";                  // Suffix fix
  frame += "1";                      // Stop bit
  return frame;
}

void transmitRawBits(const String& bitStr, uint8_t repeats = 6) {
  if (bitStr.length() == 0) return;

  for (uint8_t r = 0; r < repeats; r++) {
    for (size_t i = 0; i < bitStr.length(); i++) {
      sendPulse(bitStr.charAt(i) == '1');
    }
    digitalWrite(RF_PIN, LOW);
    delayMicroseconds(T_SYNC);
  }

  txCount++;
}

void transmitRF(bool turnOn, uint8_t repeats = 6) {
  String frame = buildFrame(currentHouseCode, turnOn);
  transmitRawBits(frame, repeats);

  heatState = turnOn;
  lastHeartbeat = millis();
  lastTxStatus = turnOn ? "HEAT ON (Pornit)" : "HEAT OFF (Oprit)";

  Serial.printf("[RF 433.92] Comandă: %s | Cadru: %s (x%d)\n", 
                lastTxStatus.c_str(), frame.c_str(), repeats);
}

// ======================= SISTEM DE FIȘIERE NVS FLASH =================
uint16_t parseHouseCode(String str) {
  str.trim();
  if (str.startsWith("0x") || str.startsWith("0X")) {
    return (uint16_t)strtol(str.c_str(), NULL, 16) & 0x0FFF;
  }
  if (str.length() == 12 && (str[0] == '0' || str[0] == '1')) {
    uint16_t val = 0;
    for (int i = 0; i < 12; i++) {
      val = (val << 1) | (str[i] == '1' ? 1 : 0);
    }
    return val & 0x0FFF;
  }
  return (uint16_t)str.toInt() & 0x0FFF;
}

void saveConfigToNVS() {
  nvs_handle_t handle;
  esp_err_t err = nvs_open("euroster", NVS_READWRITE, &handle);
  if (err == ESP_OK) {
    nvs_set_u16(handle, "hCode", currentHouseCode);
    nvs_commit(handle);
    nvs_close(handle);
    Serial.printf("[NVS Flash] Configurare salvată: 0x%03X (%u)\n", 
                  currentHouseCode, currentHouseCode);
  }
}

void loadConfigFromNVS() {
  esp_err_t err = nvs_flash_init();
  if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    nvs_flash_erase();
    nvs_flash_init();
  }

  nvs_handle_t handle;
  err = nvs_open("euroster", NVS_READONLY, &handle);
  if (err == ESP_OK) {
    uint16_t code = 0x0E60;
    if (nvs_get_u16(handle, "hCode", &code) == ESP_OK) {
      currentHouseCode = code;
    }
    nvs_close(handle);
    Serial.printf("[NVS Flash] House Code încărcat: 0x%03X (%u)\n", 
                  currentHouseCode, currentHouseCode);
  } else {
    Serial.println("[NVS Flash] Folosesc House Code implicit: 0xE60");
    saveConfigToNVS();
  }
}

// ======================= LOGICĂ SCANNER / BRUTE FORCE ===============
void addRecentScanCode(uint16_t code) {
  for (int i = min((int)recentScanCount, RECENT_SCAN_MAX - 1); i > 0; i--) {
    recentScanCodes[i] = recentScanCodes[i - 1];
  }
  recentScanCodes[0] = code;
  if (recentScanCount < RECENT_SCAN_MAX) recentScanCount++;
}

void handleScanner() {
  if (!isScanning) return;

  if (millis() - scanLastStepTime >= SCAN_STEP_INTERVAL) {
    scanLastStepTime = millis();

    String frame = buildFrame(scanCurrentCode, true);
    transmitRawBits(frame, 2);
    addRecentScanCode(scanCurrentCode);

    if (scanCurrentCode >= scanEndCode) {
      isScanning = false;
      Serial.println("[Scanner] Scanare finalizată complet.");
    } else {
      scanCurrentCode++;
    }
  }
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
    body { background-color: var(--bg); color: var(--text); display: flex; justify-content: center; align-items: flex-start; min-height: 100vh; padding: 16px; }
    .card { background-color: var(--card); border: 1px solid var(--border); border-radius: 20px; width: 100%; max-width: 480px; padding: 24px; box-shadow: 0 15px 35px rgba(0,0,0,0.6); text-align: center; }
    .header { margin-bottom: 18px; }
    h1 { font-size: 1.35rem; font-weight: 700; letter-spacing: -0.5px; }
    .subtitle { font-size: 0.85rem; color: var(--text-muted); margin-top: 4px; }
    
    .status-badge { display: inline-flex; align-items: center; justify-content: center; gap: 10px; padding: 11px 24px; border-radius: 9999px; font-weight: 700; font-size: 1.05rem; margin-bottom: 22px; transition: all 0.3s ease; }
    .status-on { background-color: var(--green-glow); color: var(--green); border: 1.5px solid var(--green); box-shadow: 0 0 15px var(--green-glow); }
    .status-off { background-color: var(--red-glow); color: var(--red); border: 1.5px solid var(--red); box-shadow: 0 0 15px var(--red-glow); }
    .dot { width: 12px; height: 12px; border-radius: 50%; }
    .dot-on { background-color: var(--green); box-shadow: 0 0 10px var(--green); }
    .dot-off { background-color: var(--red); }
    
    .btn-group { display: flex; flex-direction: column; gap: 12px; margin-bottom: 22px; }
    button { border: none; border-radius: 14px; padding: 16px; font-size: 1rem; font-weight: 700; cursor: pointer; transition: all 0.15s ease; display: flex; justify-content: center; align-items: center; gap: 10px; width: 100%; }
    button:active { transform: scale(0.97); }
    .btn-on { background: linear-gradient(135deg, #22c55e, #16a34a); color: #022c22; box-shadow: 0 4px 15px rgba(34, 197, 94, 0.35); }
    .btn-off { background: linear-gradient(135deg, #ef4444, #dc2626); color: #ffffff; box-shadow: 0 4px 15px rgba(239, 68, 68, 0.35); }
    .btn-sync { background-color: #0f172a; border: 1px solid var(--border); color: var(--text-muted); padding: 12px; font-size: 0.85rem; font-weight: 600; border-radius: 10px; }
    .btn-sync:hover { color: var(--text); border-color: var(--primary); }

    .section-title { font-size: 0.85rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: var(--primary); margin: 20px 0 10px; text-align: left; display: flex; justify-content: space-between; align-items: center; }
    .box { background: #0f172a; border: 1px solid #334155; border-radius: 14px; padding: 14px; text-align: left; margin-top: 6px; font-size: 0.82rem; }
    
    .meta-box { border-top: 1px solid var(--border); padding-top: 16px; text-align: left; font-size: 0.82rem; color: var(--text-muted); display: flex; flex-direction: column; gap: 8px; }
    .meta-row { display: flex; justify-content: space-between; align-items: center; }
    .meta-val { color: var(--text); font-weight: 600; font-family: monospace; font-size: 0.88rem; }

    .input-row { display: flex; gap: 8px; margin: 8px 0; }
    .input-text { flex: 1; background: #020617; border: 1px solid #334155; border-radius: 8px; padding: 10px; color: #38bdf8; font-family: monospace; font-size: 0.95rem; font-weight: 700; text-align: center; }
    .btn-small { padding: 9px 14px; font-size: 0.85rem; font-weight: 700; border-radius: 8px; border: none; cursor: pointer; }
    .btn-save { background: #0284c7; color: #fff; width: auto; flex: 1; }
    .btn-step { background: #1e293b; color: var(--text); border: 1px solid var(--border); width: 44px; padding: 0; }
    
    .scanner-controls { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
    .btn-scan-start { background: linear-gradient(135deg, #059669, #10b981); color: #fff; }
    .btn-scan-stop { background: linear-gradient(135deg, #dc2626, #ef4444); color: #fff; }

    .progress-bar-bg { width: 100%; height: 8px; background: #020617; border-radius: 4px; overflow: hidden; margin: 10px 0 6px; }
    .progress-bar-fill { height: 100%; width: 0%; background: var(--primary); transition: width 0.3s; }
    
    .recent-chips { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
    .chip { background: #1e293b; border: 1px solid var(--border); border-radius: 6px; padding: 4px 8px; font-family: monospace; font-size: 0.78rem; cursor: pointer; color: var(--text); }
    .chip:hover { border-color: var(--primary); color: #38bdf8; }

    .toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #1e293b; color: #fff; padding: 10px 20px; border-radius: 30px; font-size: 0.85rem; border: 1px solid var(--border); opacity: 0; pointer-events: none; transition: opacity 0.3s; z-index: 100; box-shadow: 0 5px 15px rgba(0,0,0,0.5); }
    .toast.show { opacity: 1; }
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>Euroster 2006 TX</h1>
      <div class="subtitle">Wi-Fi RF Gateway & Configurator (ESP32-C3)</div>
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

    <!-- SECTIUNEA CONFIGURARE HOUSE CODE & FLASH STORAGE -->
    <div class="section-title">
      <span>💾 House Code (Salvat în NVS Flash)</span>
      <span id="fs-badge" style="color:var(--green); font-size: 0.75rem; font-weight: normal;">Flash NVS OK</span>
    </div>
    <div class="box">
      <div class="meta-row">
        <span>Cod Activ:</span>
        <span id="active-code-display" class="meta-val" style="color:#38bdf8; font-size:1.05rem;">0xE60</span>
      </div>
      <div class="input-row">
        <button class="btn-small btn-step" onclick="stepCode(-1)" title="Cod anterior">-1</button>
        <input type="text" id="input-house-code" class="input-text" value="0xE60" maxlength="12" placeholder="0x000 - 0xFFF">
        <button class="btn-small btn-step" onclick="stepCode(1)" title="Cod următor">+1</button>
      </div>
      <div style="display:flex; gap: 8px;">
        <button class="btn-small btn-save" onclick="saveCustomCode()">💾 Salvează în Flash</button>
        <button class="btn-small btn-step" style="width:auto; padding: 0 14px;" onclick="testCurrentInput()" title="Trimite puls de test ON">🧪 Testează</button>
      </div>
    </div>

    <!-- SECTIUNEA SCANNER AUTOMAT BRUTE-FORCE -->
    <div class="section-title">
      <span>🔍 Scanner Automat (Brute-Force)</span>
      <span id="scan-state-badge" style="font-size:0.75rem; color:var(--text-muted);">Inactiv</span>
    </div>
    <div class="box">
      <div style="font-size: 0.78rem; color: var(--text-muted); line-height: 1.35;">
        Parcurge automat toate cele 4096 de coduri (0x000 - 0xFFF). Ascultă când țăcănește releul la cazan!
      </div>
      
      <div class="progress-bar-bg">
        <div id="scan-progress-bar" class="progress-bar-fill"></div>
      </div>
      <div class="meta-row" style="font-size: 0.76rem;">
        <span>Cod în scanare: <strong id="scan-live-code" style="color:#fff;">-</strong></span>
        <span id="scan-percent">0%</span>
      </div>

      <div class="scanner-controls">
        <button id="btn-scan-start" class="btn-scan-start" onclick="startScan()">
          ▶️ Pornește Scanare Automată
        </button>
        <button id="btn-scan-stop" class="btn-scan-stop" style="display:none;" onclick="stopScan()">
          🛑 AM AUZIT RELEUL! (STOP)
        </button>
      </div>

      <div id="recent-box" style="margin-top:10px; display:none;">
        <div style="font-size:0.75rem; color:var(--text-muted);">Coduri recente din jurul opririi (apasă pentru test):</div>
        <div id="recent-chips-container" class="recent-chips"></div>
      </div>
    </div>

    <!-- SECTIUNEA STARE GATEWAY -->
    <div class="section-title"><span>ℹ️ Stare Gateway</span></div>
    <div class="meta-box">
      <div class="meta-row"><span>Ultima comandă RF:</span><span id="last-tx" class="meta-val">-</span></div>
      <div class="meta-row"><span>Următorul Keepalive:</span><span id="next-ka" class="meta-val">-</span></div>
      <div class="meta-row"><span>Total transmisii RF:</span><span id="tx-count" class="meta-val">0</span></div>
      <div class="meta-row"><span>Semnal Wi-Fi (RSSI):</span><span id="wifi-rssi" class="meta-val">-</span></div>
      <div class="meta-row"><span>Timp funcționare:</span><span id="uptime" class="meta-val">-</span></div>
    </div>
  </div>

  <div id="toast" class="toast">Comandă trimisă!</div>

  <script>
    let isCurrentlyScanning = false;

    function showToast(msg) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.className = "toast show";
      setTimeout(() => { t.className = "toast"; }, 2500);
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
      
      if (data.houseCode) {
        document.getElementById('active-code-display').innerText = data.houseCode;
      }
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

    function stepCode(delta) {
      const inp = document.getElementById('input-house-code');
      let val = parseInt(inp.value, 16);
      if (isNaN(val)) val = 0;
      val = (val + delta + 4096) % 4096;
      inp.value = '0x' + val.toString(16).toUpperCase().padStart(3, '0');
    }

    async function saveCustomCode() {
      const codeStr = document.getElementById('input-house-code').value.trim();
      try {
        const res = await fetch('/api/set_house_code?code=' + encodeURIComponent(codeStr), { method: 'POST' });
        const data = await res.json();
        updateUI(data);
        showToast('💾 House Code salvat în Flash NVS: ' + data.houseCode);
      } catch (err) {
        showToast('Eroare salvare cod!');
      }
    }

    async function testCurrentInput() {
      const codeStr = document.getElementById('input-house-code').value.trim();
      try {
        await fetch('/api/test_code?code=' + encodeURIComponent(codeStr) + '&action=on', { method: 'POST' });
        showToast('🧪 Test ON trimis pentru ' + codeStr);
      } catch (err) {
        showToast('Eroare trimitere test!');
      }
    }

    async function startScan() {
      try {
        await fetch('/api/scan_start', { method: 'POST' });
        isCurrentlyScanning = true;
        document.getElementById('btn-scan-start').style.display = 'none';
        document.getElementById('btn-scan-stop').style.display = 'block';
        document.getElementById('scan-state-badge').innerText = 'În scanare activă...';
        document.getElementById('scan-state-badge').style.color = '#10b981';
        document.getElementById('recent-box').style.display = 'none';
        showToast('▶️ Scanare pornită! Ascultă releul...');
      } catch (err) {
        showToast('Eroare pornire scanare!');
      }
    }

    async function stopScan() {
      try {
        const res = await fetch('/api/scan_stop', { method: 'POST' });
        const data = await res.json();
        isCurrentlyScanning = false;
        document.getElementById('btn-scan-start').style.display = 'block';
        document.getElementById('btn-scan-stop').style.display = 'none';
        document.getElementById('scan-state-badge').innerText = 'Oprit la ' + data.stoppedAt;
        document.getElementById('scan-state-badge').style.color = '#ef4444';
        
        if (data.recent && data.recent.length > 0) {
          const container = document.getElementById('recent-chips-container');
          container.innerHTML = '';
          data.recent.forEach(c => {
            const span = document.createElement('span');
            span.className = 'chip';
            span.innerText = c;
            span.onclick = () => {
              document.getElementById('input-house-code').value = c;
              testCurrentInput();
            };
            container.appendChild(span);
          });
          document.getElementById('recent-box').style.display = 'block';
        }

        showToast('🛑 Scanare oprită la ' + data.stoppedAt);
      } catch (err) {
        showToast('Eroare oprire!');
      }
    }

    async function pollScanStatus() {
      if (!isCurrentlyScanning) return;
      try {
        const res = await fetch('/api/scan_status');
        const d = await res.json();
        if (d.scanning) {
          document.getElementById('scan-live-code').innerText = d.current;
          document.getElementById('scan-percent').innerText = d.progress + '%';
          document.getElementById('scan-progress-bar').style.width = d.progress + '%';
        } else {
          stopScan();
        }
      } catch (err) {}
    }

    async function pollStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        updateUI(data);
      } catch (err) {}
    }

    setInterval(pollStatus, 3000);
    setInterval(pollScanStatus, 450);
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

  char codeHex[10];
  snprintf(codeHex, sizeof(codeHex), "0x%03X", currentHouseCode);

  String json = "{";
  json += "\"state\":" + String(heatState ? "true" : "false") + ",";
  json += "\"lastTx\":\"" + lastTxStatus + "\",";
  json += "\"count\":" + String(txCount) + ",";
  json += "\"nextKa\":" + String(remainingKa) + ",";
  json += "\"rssi\":" + String(WiFi.RSSI()) + ",";
  json += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
  json += "\"uptime\":" + String(millis() / 1000) + ",";
  json += "\"houseCode\":\"" + String(codeHex) + " (" + String(currentHouseCode) + ")\"";
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

void handleApiSetHouseCode() {
  if (server.hasArg("code")) {
    currentHouseCode = parseHouseCode(server.arg("code"));
    saveConfigToNVS();
  }
  sendJsonResponse();
}

void handleApiTestCode() {
  uint16_t code = currentHouseCode;
  if (server.hasArg("code")) {
    code = parseHouseCode(server.arg("code"));
  }
  bool turnOn = (!server.hasArg("action") || server.arg("action") == "on");
  String frame = buildFrame(code, turnOn);
  transmitRawBits(frame, 4);
  Serial.printf("[Test] Trimis cod test 0x%03X (%s)\n", code, turnOn ? "ON" : "OFF");
  sendJsonResponse();
}

void handleApiScanStart() {
  scanCurrentCode = 0x0000;
  scanEndCode = 0x0FFF;
  recentScanCount = 0;
  isScanning = true;
  scanLastStepTime = millis();
  Serial.println("[Scanner] Pornit scanare automată 0x000 - 0xFFF");
  server.send(200, "application/json", "{\"status\":\"started\"}");
}

void handleApiScanStop() {
  isScanning = false;
  char stoppedBuf[12];
  snprintf(stoppedBuf, sizeof(stoppedBuf), "0x%03X", scanCurrentCode);

  String json = "{\"status\":\"stopped\",\"stoppedAt\":\"" + String(stoppedBuf) + "\",\"recent\":[";
  for (uint8_t i = 0; i < recentScanCount; i++) {
    if (i > 0) json += ",";
    char cBuf[10];
    snprintf(cBuf, sizeof(cBuf), "\"0x%03X\"", recentScanCodes[i]);
    json += cBuf;
  }
  json += "]}";

  Serial.printf("[Scanner] Oprit la %s\n", stoppedBuf);
  server.send(200, "application/json", json);
}

void handleApiScanStatus() {
  char curBuf[12];
  snprintf(curBuf, sizeof(curBuf), "0x%03X", scanCurrentCode);
  float progress = ((float)scanCurrentCode / 4095.0f) * 100.0f;

  String json = "{";
  json += "\"scanning\":" + String(isScanning ? "true" : "false") + ",";
  json += "\"current\":\"" + String(curBuf) + "\",";
  json += "\"currentDec\":" + String(scanCurrentCode) + ",";
  json += "\"progress\":" + String(progress, 1);
  json += "}";

  server.send(200, "application/json", json);
}

// ======================= SETUP ȘI LOOP ==============================
void setup() {
  Serial.begin(115200);
  
  // 1. Configurare Pin TX RF 433 MHz (Ieșire)
  pinMode(RF_PIN, OUTPUT);
  digitalWrite(RF_PIN, LOW);

  delay(400);
  Serial.println("\n=============================================");
  Serial.println("  Euroster 2006 TX Wi-Fi Gateway & Scanner   ");
  Serial.println("=============================================");

  // 2. Încărcare House Code din Flash NVS (Non-Volatile Storage)
  loadConfigFromNVS();

  // 3. Initializare mod Station curat cu auto-reconnect
  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  WiFi.mode(WIFI_STA);

  // 4. Fortare 802.11 b/g/n (compatibilitate maxima)
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);

  // 5. Conectare Wi-Fi
  Serial.printf("[WiFi] Conectare la '%s'...", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  // 6. Setare TX Power la 8.5 dBm
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
    server.on("/api/set_house_code", HTTP_POST, handleApiSetHouseCode);
    server.on("/api/test_code", HTTP_POST, handleApiTestCode);
    server.on("/api/scan_start", HTTP_POST, handleApiScanStart);
    server.on("/api/scan_stop", HTTP_POST, handleApiScanStop);
    server.on("/api/scan_status", HTTP_GET, handleApiScanStatus);

    server.begin();
    Serial.println("[HTTP] Serverul web a pornit.");
  } else {
    Serial.printf("[WiFi] Esec conectare initiala. Cod status: %d\n", WiFi.status());
  }

  lastHeartbeat = millis();
}

void loop() {
  // 1. Dacă scanerul este activ, emite coduri pas cu pas
  handleScanner();

  // 2. Gestionare cereri HTTP
  if (WiFi.status() == WL_CONNECTED) {
    server.handleClient();
  } else {
    if (millis() - lastWifiRetry >= 10000) {
      lastWifiRetry = millis();
      Serial.println("[WiFi] Reîncercare conectare...");
      WiFi.reconnect();
    }
  }

  // 3. Keepalive automat la fiecare 45s (doar dacă nu este în scanare activă)
  if (!isScanning && millis() - lastHeartbeat >= HEARTBEAT_INTERVAL) {
    Serial.println("[RF 433.92] Trimitere puls Keepalive automat...");
    transmitRF(heatState, 3);
  }
}