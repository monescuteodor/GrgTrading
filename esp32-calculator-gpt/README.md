# Calculator + GPT (Groq) pe ESP32

Calculator de buzunar pe un **ESP32 NodeMCU** cu ecran **SPI 1.8" 128x160 (ST7735)**
și o **tastatură membrană 4x4**. Are două moduri:

- **Calculator**: + − × ÷, paranteze, putere, radical, zecimale, `Ans`.
- **GPT**: pui o întrebare și răspunsul vine de la un model AI prin API-ul **Groq**.
  Întrebarea o scrii de pe telefon, într-o pagină web servită chiar de ESP32,
  sau din Serial Monitor. Răspunsul apare pe ecran (și pe telefon).

## Ce îți trebuie

| Componentă | Note |
|---|---|
| ESP32 NodeMCU / DevKit (WROOM) | |
| Ecran 1.8" SPI 128x160 ST7735 | pini: VCC, GND, CS, RESET, A0, SDA, SCK, LED |
| Tastatură membrană 4x4 | 8 fire |
| Fire dupont | |

## Conexiuni

### Ecran ST7735 → ESP32

| Pin ecran | ESP32 |
|---|---|
| VCC | 3V3 |
| GND | GND |
| CS | GPIO 5 |
| RESET | GPIO 17 |
| A0 (sau DC / RS) | GPIO 16 |
| SDA (MOSI) | GPIO 23 |
| SCK | GPIO 18 |
| LED (iluminare) | 3V3 |

### Tastatură 4x4 → ESP32

Ținând tastatura cu fața la tine, firele de la stânga la dreapta:

| Fir | 1 (R1) | 2 (R2) | 3 (R3) | 4 (R4) | 5 (C1) | 6 (C2) | 7 (C3) | 8 (C4) |
|---|---|---|---|---|---|---|---|---|
| ESP32 | GPIO 13 | GPIO 14 | GPIO 27 | GPIO 26 | GPIO 25 | GPIO 33 | GPIO 32 | GPIO 4 |

Dacă tastele apasă altceva decât scrie pe ele (de exemplu `1` dă `A`), ai firele
în ordine inversă: inversează-le sau schimbă `rowPins` / `colPins` în cod.

## Instalare (Arduino IDE)

1. **File → Preferences → Additional boards manager URLs**:
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
2. **Tools → Board → Boards Manager**: instalează **esp32** (Espressif Systems).
3. **Tools → Manage Libraries**: instalează
   - `Adafruit ST7735 and ST7789 Library` (acceptă și `Adafruit GFX Library`)
   - `Keypad` (Mark Stanley, Alexander Brevig)
   - `ArduinoJson` (Benoit Blanchon, versiunea 7.x)
4. Fă-ți o cheie API gratuită la <https://console.groq.com/keys>.
5. În folderul `calculator_gpt/`, copiază `config.example.h` ca **`config.h`** și
   completează WiFi-ul (doar 2.4 GHz) și cheia Groq.
6. Deschide `calculator_gpt/calculator_gpt.ino`, alege placa **ESP32 Dev Module**
   și portul, apoi Upload.

`config.h` e în `.gitignore`, ca parola și cheia să nu ajungă pe GitHub.

## Taste

Fiecare tastă are o funcție la **apăsare scurtă** și alta la **apăsare lungă** (~0.5 s).

### Mod calculator

| Tastă | Scurt | Lung |
|---|---|---|
| 0–9 | cifra | `0` → Ans, `1` → √ |
| A | + | ( |
| B | − | ) |
| C | × | ^ (putere) |
| D | ÷ | AC (șterge tot) |
| * | . (virgulă) | DEL (șterge ultimul caracter) |
| # | = | trece în modul GPT |

- Înmulțirea implicită merge: `2(3+1)` = 8, `3√9` = 9.
- Dacă după `=` apeși un operator, calculul continuă de la rezultat (`Ans+...`);
  dacă apeși o cifră, începi un calcul nou.
- Parantezele rămase deschise la final se închid automat.

### Mod GPT

| Tastă | Acțiune |
|---|---|
| 2 sau A | derulează în sus |
| 8 sau B | derulează în jos |
| * lung | chat nou (uită conversația) |
| # lung | înapoi la calculator |

Ca să întrebi ceva, deschide pe telefon (conectat la același WiFi) adresa
afișată pe ecran, de exemplu `http://192.168.1.23`, sau `http://calculator.local`.
Poți scrie întrebarea și în **Serial Monitor** (115200 baud, cu „Newline”).
Calculatorul ține minte ultimele 3 întrebări, ca să poți pune întrebări de continuare.

## Probleme frecvente

- **Ecran alb**: verifică CS, A0, RESET și SCK/SDA. LED-ul trebuie legat la 3V3.
- **Culori inversate sau zgomot pe margini**: în `config.h`, schimbă `TFT_INIT_TAB`
  în `INITR_GREENTAB` sau `INITR_REDTAB`.
- **Ecran cu susul în jos**: pune `tft.setRotation(2)` în `setup()`.
- **„Eroare 401”**: cheia Groq e greșită.
- **„model decommissioned” / „model not found”**: alege un model din
  <https://console.groq.com/docs/models> și pune-l la `GROQ_MODEL`.
- **„Eroare 429”**: ai atins limita gratuită; așteaptă puțin.
- **Diacriticele** se afișează fără căciulițe (ă → a), fiindcă fontul ecranului nu le are.
- **Placă cu modul WROVER**: acolo GPIO 16/17 sunt folosiți de PSRAM; mută
  `TFT_DC` și `TFT_RST`, de exemplu pe GPIO 21 și 22.

## Securitate

Conexiunea HTTPS la Groq folosește `setInsecure()`: e criptată, dar certificatul
serverului nu e verificat. E suficient pentru un proiect de acasă. Dacă vrei
verificare completă, înlocuiește linia cu `client.setCACert(...)` și certificatul
rădăcină al `api.groq.com`.

Pagina web nu are parolă: oricine e în aceeași rețea WiFi poate pune întrebări
(pe cheia ta Groq).

## Test pentru evaluator (opțional, pe PC)

```
g++ -I calculator_gpt test/test_calc_eval.cpp -o test_calc && ./test_calc
```
