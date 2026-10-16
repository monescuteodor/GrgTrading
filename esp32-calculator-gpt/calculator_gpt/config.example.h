// Copiaza acest fisier ca "config.h" (in acelasi folder) si completeaza datele.
// config.h este in .gitignore, ca sa nu urci parola WiFi / cheia API pe GitHub.
#pragma once

// --- WiFi (doar 2.4 GHz, ESP32 nu vede retele de 5 GHz) ---
#define WIFI_SSID     "NumeleReteleiTale"
#define WIFI_PASSWORD "ParolaWiFi"

// --- Groq: cheia o iei gratuit de la https://console.groq.com/keys ---
#define GROQ_API_KEY  "gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

// Modelul folosit. Lista actuala: https://console.groq.com/docs/models
// Daca primesti eroare "model decommissioned", pune aici un model din lista.
#define GROQ_MODEL    "llama-3.3-70b-versatile"

// Instructiunile pentru AI (raspunsuri scurte, ca sa incapa pe ecran).
#define SYSTEM_PROMPT "Esti un asistent pe un calculator de buzunar cu ecran mic. " \
                      "Raspunde in limba intrebarii, scurt si clar (maxim 80 de cuvinte), " \
                      "fara markdown, fara tabele si fara emoji."

// Numele in retea: pagina va fi si la http://calculator.local
#define HOSTNAME      "calculator"

// Varianta de initializare a ecranului ST7735.
// Daca vezi culori inversate, margini cu "zgomot" sau imaginea decalata,
// incearca pe rand: INITR_BLACKTAB, INITR_GREENTAB, INITR_REDTAB
#define TFT_INIT_TAB  INITR_BLACKTAB
