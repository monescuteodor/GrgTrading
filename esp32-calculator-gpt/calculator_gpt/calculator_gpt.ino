// Calculator + GPT (Groq) pe ESP32 NodeMCU cu ecran ST7735 128x160 si tastatura 4x4.
//
// Biblioteci (Arduino IDE -> Library Manager):
//   Adafruit ST7735 and ST7789 Library, Adafruit GFX Library, Keypad (Mark Stanley), ArduinoJson (v7)
// Placa: "ESP32 Dev Module" (pachetul esp32 de la Espressif)

#include <Arduino.h>
#include <SPI.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7735.h>
#include <Keypad.h>
#include <vector>

#if __has_include("config.h")
#include "config.h"
#else
#error "Copiaza config.example.h ca config.h si completeaza WiFi + cheia Groq"
#endif

#include "calc_eval.h"

// ---------------- Pini ----------------
// Ecran (SPI hardware VSPI): SCK -> GPIO18, SDA -> GPIO23
#define TFT_CS   5
#define TFT_DC   16   // pinul "A0" (sau "DC"/"RS") de pe ecran
#define TFT_RST  17

// Tastatura 4x4: primii 4 fire = randuri, ultimii 4 = coloane
const byte ROWS = 4, COLS = 4;
char keyMap[ROWS][COLS] = {
  {'1', '2', '3', 'A'},
  {'4', '5', '6', 'B'},
  {'7', '8', '9', 'C'},
  {'*', '0', '#', 'D'},
};
byte rowPins[ROWS] = {13, 14, 27, 26};
byte colPins[COLS] = {25, 33, 32, 4};

// ---------------- Culori / layout ----------------
#define C_BLACK  ST77XX_BLACK
#define C_WHITE  ST77XX_WHITE
#define C_GREY   0x8410
#define C_DGREY  0x4208
#define C_RED    ST77XX_RED
#define C_GREEN  ST77XX_GREEN
#define C_YELLOW ST77XX_YELLOW
#define C_CYAN   ST77XX_CYAN
#define C_HDR_CALC 0x0010   // albastru inchis
#define C_HDR_GPT  0x0320   // verde inchis

const int HEADER_H = 14;
const int GPT_TOP = 18, GPT_LINE_H = 10, GPT_ROWS = 13, GPT_COLS = 21;

Adafruit_ST7735 tft(TFT_CS, TFT_DC, TFT_RST);
Keypad keypad = Keypad(makeKeymap(keyMap), rowPins, colPins, ROWS, COLS);
WebServer server(80);

enum AppMode { MODE_CALC, MODE_GPT };
AppMode mode = MODE_CALC;

// ---------------- Stare calculator ----------------
String expr;              // expresia in simboluri interne (vezi calc_eval.h)
String resultStr;         // ultimul rezultat formatat
const char* calcError = nullptr;
double ansValue = 0;
bool justEvaluated = false;
const unsigned MAX_EXPR = 60;

// ---------------- Stare GPT ----------------
struct Line { String text; uint16_t color; };
struct Turn { String q, a; };
std::vector<Line> gptLines;
std::vector<Turn> history;     // ultimele intrebari/raspunsuri, pentru context
const size_t MAX_HISTORY = 3;
int gptScroll = 0;
bool gptHasContent = false;
String serialBuf;

// =====================================================================
//  Utilitare text
// =====================================================================

// Ecranul are doar fontul CP437, deci convertim UTF-8 (diacritice etc.).
String mapCodepoint(uint32_t cp) {
  if (cp == '\n') return "\n";
  if (cp == '\t') return " ";
  if (cp >= 0x20 && cp < 0x7F) return String((char)cp);
  switch (cp) {
    case 0x103: case 0xE2: case 0xE1: case 0xE0: case 0xE4: return "a";
    case 0x102: case 0xC2: case 0xC1: case 0xC0: case 0xC4: return "A";
    case 0xEE: case 0xED: return "i";
    case 0xCE: case 0xCD: return "I";
    case 0x219: case 0x15F: return "s";
    case 0x218: case 0x15E: return "S";
    case 0x21B: case 0x163: return "t";
    case 0x21A: case 0x162: return "T";
    case 0xE9: case 0xE8: case 0xEB: return "e";
    case 0xC9: return "E";
    case 0xF3: case 0xF6: return "o";
    case 0xFA: case 0xFC: return "u";
    case 0x2018: case 0x2019: return "'";
    case 0x201C: case 0x201D: case 0x201E: return "\"";
    case 0x2013: case 0x2014: case 0x2212: return "-";
    case 0x2026: return "...";
    case 0x2022: return "*";
    case 0xB0: return String((char)0xF8);    // grad
    case 0xB2: return String((char)0xFD);    // la patrat
    case 0xD7: return "x";
    case 0xF7: return String((char)0xF6);    // impartit
    case 0x221A: return String((char)0xFB);  // radical
    case 0x3C0: return String((char)0xE3);   // pi
    case 0xA0: return " ";
    default: return (cp < 0x20) ? "" : "?";
  }
}

String toDisplayText(const String& in) {
  String out;
  out.reserve(in.length());
  const uint8_t* s = (const uint8_t*)in.c_str();
  size_t n = in.length();
  for (size_t i = 0; i < n;) {
    uint8_t c = s[i];
    uint32_t cp;
    if (c < 0x80) { cp = c; i += 1; }
    else if ((c & 0xE0) == 0xC0 && i + 1 < n) { cp = ((c & 0x1F) << 6) | (s[i + 1] & 0x3F); i += 2; }
    else if ((c & 0xF0) == 0xE0 && i + 2 < n) { cp = ((c & 0x0F) << 12) | ((s[i + 1] & 0x3F) << 6) | (s[i + 2] & 0x3F); i += 3; }
    else if ((c & 0xF8) == 0xF0 && i + 3 < n) { i += 4; continue; }  // emoji: le sarim
    else { i += 1; continue; }
    out += mapCodepoint(cp);
  }
  // curatam putin markdown, in caz ca modelul il foloseste oricum
  out.replace("**", "");
  out.replace("__", "");
  out.replace("`", "");
  out.replace("### ", "");
  out.replace("## ", "");
  out.replace("# ", "");
  return out;
}

// Imparte textul in randuri de maxim GPT_COLS caractere, pe cuvinte.
void wrapInto(const String& text, uint16_t color, std::vector<Line>& out) {
  int start = 0;
  int len = text.length();
  while (start <= len) {
    int nl = text.indexOf('\n', start);
    if (nl < 0) nl = len;
    String para = text.substring(start, nl);
    para.trim();
    if (para.length() == 0) {
      out.push_back({"", color});
    } else {
      String line;
      int p = 0;
      while (p < (int)para.length()) {
        int sp = para.indexOf(' ', p);
        if (sp < 0) sp = para.length();
        String word = para.substring(p, sp);
        p = sp + 1;
        if (word.length() == 0) continue;
        while (word.length() > (unsigned)GPT_COLS) {  // cuvant prea lung
          if (line.length()) { out.push_back({line, color}); line = ""; }
          out.push_back({word.substring(0, GPT_COLS), color});
          word = word.substring(GPT_COLS);
        }
        if (line.length() == 0) line = word;
        else if (line.length() + 1 + word.length() <= (unsigned)GPT_COLS) line += " " + word;
        else { out.push_back({line, color}); line = word; }
      }
      if (line.length()) out.push_back({line, color});
    }
    start = nl + 1;
  }
}

String formatNumber(double v) {
  char buf[32];
  if (fabs(v) < 1e15 && v == floor(v)) {
    snprintf(buf, sizeof(buf), "%lld", (long long)v);
  } else {
    snprintf(buf, sizeof(buf), "%.10g", v);
  }
  return String(buf);
}

// Simboluri interne -> ce se vede pe ecran
String prettyExpr(const String& e) {
  String o;
  for (unsigned i = 0; i < e.length(); i++) {
    char c = e[i];
    switch (c) {
      case '*': o += 'x'; break;
      case '/': o += (char)0xF6; break;  // ÷ in CP437
      case 'r': o += (char)0xFB; break;  // √ in CP437
      case 'a': o += "Ans"; break;
      default: o += c;
    }
  }
  return o;
}

// =====================================================================
//  Desenare
// =====================================================================

void drawHeader(const char* title, uint16_t bg) {
  tft.fillRect(0, 0, tft.width(), HEADER_H, bg);
  tft.setTextSize(1);
  tft.setTextColor(C_WHITE);
  tft.setCursor(4, 3);
  tft.print(title);
  bool ok = WiFi.status() == WL_CONNECTED;
  tft.setTextColor(ok ? C_WHITE : C_RED);
  tft.setCursor(tft.width() - 4 - 6 * 4, 3);
  tft.print(ok ? "WiFi" : "----");
}

void drawCalc() {
  const int W = tft.width(), H = tft.height();
  drawHeader("CALCULATOR", C_HDR_CALC);
  tft.fillRect(0, HEADER_H, W, H - HEADER_H, C_BLACK);

  // Expresia: font marime 2 (12x16 px) -> 10 caractere pe rand, ultimele 4 randuri
  String pe = prettyExpr(expr);
  if (!justEvaluated) pe += '_';
  const int cpl = 10, maxLines = 4;
  int total = max(1, (int)((pe.length() + cpl - 1) / cpl));
  int first = max(0, total - maxLines);
  tft.setTextSize(2);
  tft.setTextColor(justEvaluated ? C_GREY : C_WHITE);
  for (int l = first; l < total; l++) {
    tft.setCursor(4, 20 + (l - first) * 18);
    tft.print(pe.substring(l * cpl, min((int)pe.length(), (l + 1) * cpl)));
  }

  // Rezultatul, aliniat la dreapta
  if (calcError) {
    tft.setTextSize(1);
    tft.setTextColor(C_RED);
    tft.setCursor(4, 104);
    tft.print(calcError);
  } else if (resultStr.length()) {
    String r = "=" + resultStr;
    int size = r.length() <= 10 ? 2 : 1;
    tft.setTextSize(size);
    tft.setTextColor(C_GREEN);
    tft.setCursor(max(0, W - 4 - (int)r.length() * 6 * size), 100);
    tft.print(r);
  }

  // Ajutor
  tft.drawFastHLine(0, 121, W, C_DGREY);
  tft.setTextSize(1);
  tft.setTextColor(C_GREY);
  tft.setCursor(1, 125);
  tft.print("A+ B- Cx D");
  tft.print((char)0xF6);
  tft.print(" *. #=");
  tft.setCursor(1, 137);
  tft.print("Lung:*DEL DAC #GPT");
  tft.setCursor(1, 149);
  tft.print("Lung:A( B) C^ 1");
  tft.print((char)0xFB);
  tft.print(" 0Ans");
}

void buildGptIntro() {
  gptLines.clear();
  if (WiFi.status() != WL_CONNECTED) {
    wrapInto("Nu sunt conectat la WiFi.", C_RED, gptLines);
    gptLines.push_back({"", C_WHITE});
    wrapInto("Verifica WIFI_SSID si WIFI_PASSWORD din config.h (doar retele 2.4 GHz).", C_WHITE, gptLines);
    return;
  }
  wrapInto("Intreaba de pe telefon (acelasi WiFi):", C_WHITE, gptLines);
  gptLines.push_back({"", C_WHITE});
  gptLines.push_back({"http://" + WiFi.localIP().toString(), C_CYAN});
  gptLines.push_back({String(HOSTNAME) + ".local", C_CYAN});
  gptLines.push_back({"", C_WHITE});
  wrapInto("Sau scrie in Serial Monitor (115200).", C_WHITE, gptLines);
  gptLines.push_back({"", C_WHITE});
  gptLines.push_back({"2 / 8 : derulare", C_GREY});
  gptLines.push_back({"Lung * : chat nou", C_GREY});
  gptLines.push_back({"Lung # : calculator", C_GREY});
}

void drawGpt() {
  const int W = tft.width(), H = tft.height();
  drawHeader("GPT (Groq)", C_HDR_GPT);
  tft.fillRect(0, HEADER_H, W, H - HEADER_H, C_BLACK);
  if (!gptHasContent) buildGptIntro();

  int maxScroll = max(0, (int)gptLines.size() - GPT_ROWS);
  gptScroll = constrain(gptScroll, 0, maxScroll);

  tft.setTextSize(1);
  for (int i = 0; i < GPT_ROWS && gptScroll + i < (int)gptLines.size(); i++) {
    const Line& ln = gptLines[gptScroll + i];
    tft.setTextColor(ln.color);
    tft.setCursor(2, GPT_TOP + i * GPT_LINE_H);
    tft.print(ln.text);
  }

  if (maxScroll > 0) {
    // bara de derulare in dreapta
    int areaH = GPT_ROWS * GPT_LINE_H;
    int barH = max(8, areaH * GPT_ROWS / (int)gptLines.size());
    int barY = GPT_TOP + (areaH - barH) * gptScroll / maxScroll;
    tft.fillRect(W - 2, GPT_TOP, 2, areaH, C_DGREY);
    tft.fillRect(W - 2, barY, 2, barH, C_GREY);
  }
}

void drawScreen() {
  if (mode == MODE_CALC) drawCalc();
  else drawGpt();
}

// =====================================================================
//  Calculator
// =====================================================================

void calcInput(char c) {
  if (justEvaluated) {
    justEvaluated = false;
    // dupa "=", un operator continua de la rezultat (Ans), o cifra incepe o expresie noua
    expr = (strchr("+-*/^", c) && !calcError) ? "a" : "";
    resultStr = "";
    calcError = nullptr;
  }
  if (expr.length() < MAX_EXPR) expr += c;
  drawCalc();
}

void calcBackspace() {
  if (justEvaluated) {
    justEvaluated = false;
    resultStr = "";
    calcError = nullptr;
  }
  if (expr.length()) expr.remove(expr.length() - 1);
  drawCalc();
}

void calcClear() {
  expr = "";
  resultStr = "";
  calcError = nullptr;
  justEvaluated = false;
  drawCalc();
}

void calcEvaluate() {
  if (expr.length() == 0) return;
  EvalResult r = evaluateExpression(expr.c_str(), ansValue);
  if (r.ok) {
    ansValue = r.value;
    resultStr = formatNumber(r.value);
    calcError = nullptr;
  } else {
    resultStr = "";
    calcError = r.error;
  }
  justEvaluated = true;
  drawCalc();
}

// =====================================================================
//  GPT prin Groq
// =====================================================================

String askGroq(const String& question, bool& ok) {
  ok = false;
  if (WiFi.status() != WL_CONNECTED) return "Nu sunt conectat la WiFi.";

  WiFiClientSecure client;
  client.setInsecure();  // nu verificam certificatul (simplu; vezi README)
  HTTPClient http;
  http.setTimeout(30000);
  if (!http.begin(client, "https://api.groq.com/openai/v1/chat/completions")) {
    return "Nu pot porni conexiunea HTTPS.";
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + GROQ_API_KEY);

  JsonDocument req;
  req["model"] = GROQ_MODEL;
  req["max_completion_tokens"] = 400;
  req["temperature"] = 0.5;
  JsonArray msgs = req["messages"].to<JsonArray>();
  JsonObject sys = msgs.add<JsonObject>();
  sys["role"] = "system";
  sys["content"] = SYSTEM_PROMPT;
  for (const Turn& t : history) {
    JsonObject u = msgs.add<JsonObject>();
    u["role"] = "user";
    u["content"] = t.q;
    JsonObject a = msgs.add<JsonObject>();
    a["role"] = "assistant";
    a["content"] = t.a;
  }
  JsonObject u = msgs.add<JsonObject>();
  u["role"] = "user";
  u["content"] = question;

  String body;
  serializeJson(req, body);
  int code = http.POST(body);
  if (code <= 0) {
    String err = "Eroare conexiune: " + http.errorToString(code);
    http.end();
    return err;
  }
  String resp = http.getString();
  http.end();

  if (code != 200) {
    JsonDocument e;
    if (!deserializeJson(e, resp) && e["error"]["message"].is<const char*>()) {
      return "Eroare " + String(code) + ": " + e["error"]["message"].as<String>();
    }
    return "Eroare HTTP " + String(code);
  }

  JsonDocument filter;
  filter["choices"][0]["message"]["content"] = true;
  JsonDocument doc;
  if (deserializeJson(doc, resp, DeserializationOption::Filter(filter))) {
    return "Raspuns JSON invalid.";
  }
  const char* content = doc["choices"][0]["message"]["content"];
  if (!content) return "Raspuns gol de la model.";
  ok = true;
  String answer(content);
  answer.trim();
  return answer;
}

// Afiseaza intrebarea, cere raspunsul, il afiseaza. Returneaza raspunsul (sau eroarea).
String processQuestion(const String& question, bool& ok) {
  mode = MODE_GPT;
  gptHasContent = true;
  gptLines.clear();
  wrapInto("> " + toDisplayText(question), C_YELLOW, gptLines);
  gptLines.push_back({"", C_WHITE});
  gptLines.push_back({"Se gandeste...", C_CYAN});
  gptScroll = 0;
  drawGpt();

  Serial.println("> " + question);
  String answer = askGroq(question, ok);
  Serial.println(answer);
  Serial.println();

  gptLines.pop_back();  // scoatem "Se gandeste..."
  wrapInto(toDisplayText(answer), ok ? C_WHITE : C_RED, gptLines);

  if (ok) {
    history.push_back({question, answer});
    if (history.size() > MAX_HISTORY) history.erase(history.begin());
  }
  drawGpt();
  return answer;
}

void newChat() {
  history.clear();
  gptHasContent = false;
  gptScroll = 0;
  drawGpt();
}

// =====================================================================
//  Tastatura: apasare scurta vs. lunga
// =====================================================================

void onShortPress(char k) {
  if (mode == MODE_CALC) {
    switch (k) {
      case 'A': calcInput('+'); break;
      case 'B': calcInput('-'); break;
      case 'C': calcInput('*'); break;
      case 'D': calcInput('/'); break;
      case '*': calcInput('.'); break;
      case '#': calcEvaluate(); break;
      default:  calcInput(k);   // cifre
    }
  } else {
    if (k == '2' || k == 'A') { gptScroll -= 3; drawGpt(); }
    else if (k == '8' || k == 'B') { gptScroll += 3; drawGpt(); }
  }
}

void onLongPress(char k) {
  if (k == '#') {
    mode = (mode == MODE_CALC) ? MODE_GPT : MODE_CALC;
    drawScreen();
    return;
  }
  if (mode == MODE_CALC) {
    switch (k) {
      case '*': calcBackspace(); break;
      case 'D': calcClear(); break;
      case 'A': calcInput('('); break;
      case 'B': calcInput(')'); break;
      case 'C': calcInput('^'); break;
      case '1': calcInput('r'); break;
      case '0': calcInput('a'); break;
    }
  } else if (k == '*') {
    newChat();
  }
}

bool holdFired = false;
wl_status_t lastWifi = WL_IDLE_STATUS;

void onKeypadEvent(KeypadEvent key) {
  switch (keypad.getState()) {
    case PRESSED:  holdFired = false; break;
    case HOLD:     holdFired = true; onLongPress(key); break;
    case RELEASED: if (!holdFired) onShortPress(key); break;
    default: break;
  }
}

// =====================================================================
//  Pagina web
// =====================================================================

const char INDEX_HTML[] PROGMEM = R"HTML(<!doctype html>
<html lang="ro"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calculator GPT</title>
<style>
body{font-family:system-ui,sans-serif;max-width:560px;margin:0 auto;padding:16px;background:#111;color:#eee}
textarea{width:100%;box-sizing:border-box;height:90px;font-size:16px;padding:8px;border-radius:8px;border:1px solid #444;background:#1c1c1c;color:#eee}
button{margin-top:8px;padding:10px 16px;font-size:16px;border:0;border-radius:8px;background:#2a7;color:#fff}
button.sec{background:#444}button:disabled{opacity:.5}
#log div{margin:12px 0;white-space:pre-wrap;line-height:1.4}.q{color:#fc6}.e{color:#f66}
</style></head><body>
<h2>Calculator GPT</h2>
<div id="log"></div>
<textarea id="q" placeholder="Scrie intrebarea... (Enter = trimite)"></textarea><br>
<button id="send">Trimite</button> <button class="sec" id="reset">Chat nou</button>
<script>
const q=document.getElementById('q'),log=document.getElementById('log'),send=document.getElementById('send');
function add(c,t){const d=document.createElement('div');d.className=c;d.textContent=t;log.appendChild(d);d.scrollIntoView();return d;}
async function ask(){
  const t=q.value.trim(); if(!t||send.disabled) return;
  send.disabled=true; add('q','> '+t); q.value='';
  const a=add('','...');
  try{const r=await fetch('/ask',{method:'POST',body:new URLSearchParams({q:t})});
      a.textContent=await r.text(); if(!r.ok) a.className='e';}
  catch(e){a.textContent='Eroare: '+e; a.className='e';}
  send.disabled=false; q.focus();
}
send.onclick=ask;
q.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();ask();}});
document.getElementById('reset').onclick=async()=>{await fetch('/reset',{method:'POST'});log.innerHTML='';};
</script></body></html>)HTML";

void handleRoot() {
  server.send_P(200, "text/html; charset=utf-8", INDEX_HTML);
}

void handleAsk() {
  String q = server.arg("q");
  q.trim();
  if (q.isEmpty()) {
    server.send(400, "text/plain; charset=utf-8", "Intrebarea e goala.");
    return;
  }
  bool ok;
  String a = processQuestion(q, ok);
  server.send(ok ? 200 : 502, "text/plain; charset=utf-8", a);
}

void handleReset() {
  if (mode == MODE_GPT) newChat();
  else { history.clear(); gptHasContent = false; }
  server.send(200, "text/plain", "ok");
}

// =====================================================================
//  Setup / loop
// =====================================================================

void connectWiFi() {
  tft.fillScreen(C_BLACK);
  tft.setTextSize(1);
  tft.setTextColor(C_WHITE);
  tft.setCursor(4, 60);
  tft.print("Conectare la WiFi...");
  tft.setCursor(4, 74);
  tft.setTextColor(C_GREY);
  tft.print(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(250);
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("WiFi conectat. Deschide http://");
    Serial.println(WiFi.localIP());
    MDNS.begin(HOSTNAME);
  } else {
    Serial.println("WiFi nu s-a conectat. Calculatorul merge oricum.");
  }
}

void setup() {
  Serial.begin(115200);

  tft.initR(TFT_INIT_TAB);
  tft.setRotation(0);  // vertical 128x160; pune 2 daca il ai cu susul in jos
  tft.cp437(true);
  tft.setTextWrap(false);

  keypad.addEventListener(onKeypadEvent);
  keypad.setHoldTime(500);
  keypad.setDebounceTime(20);

  connectWiFi();

  server.on("/", HTTP_GET, handleRoot);
  server.on("/ask", HTTP_POST, handleAsk);
  server.on("/reset", HTTP_POST, handleReset);
  server.begin();

  lastWifi = WiFi.status();
  drawScreen();
}

void loop() {
  keypad.getKey();  // declanseaza onKeypadEvent
  server.handleClient();

  // Intrebari din Serial Monitor
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      serialBuf.trim();
      if (serialBuf.length()) {
        bool ok;
        processQuestion(serialBuf, ok);
      }
      serialBuf = "";
    } else if (serialBuf.length() < 500) {
      serialBuf += c;
    }
  }

  // Redesenam cand se schimba starea WiFi (indicatorul din colt, IP-ul)
  static unsigned long lastCheck = 0;
  if (millis() - lastCheck > 2000) {
    lastCheck = millis();
    wl_status_t st = WiFi.status();
    if ((st == WL_CONNECTED) != (lastWifi == WL_CONNECTED)) {
      if (st == WL_CONNECTED) MDNS.begin(HOSTNAME);
      drawScreen();
    }
    lastWifi = st;
  }
}
