# Anrufwächter — Webex-Middleware für verpasste Anrufe in Warteschlangen

Hört bei Webex mit: sobald ein Anruf in einer konfigurierten Anrufwarteschlange
(Call Queue) abbricht, ohne dass jemand rangegangen ist, wird das erkannt und

- eine interaktive Adaptive Card in eine Webex-Space gepostet (optional — nur
  falls eine Webex-Messaging-Lizenz vorhanden ist), und/oder
- eine E-Mail mit Anrufer-Nummer, Uhrzeit und Rückruf-Link verschickt.

Ein Klick auf "Übernehmen" in der Karte markiert den Anruf für alle sofort als
erledigt (atomarer Claim, kein doppelter Rückruf).

Reagiert auf das echte "aufgelegt, ohne beantwortet zu werden"-Ereignis —
nicht auf eine Ansage/Voicemail, kein Warten auf einen Piepton nötig.

## Dateien

| Datei                       | Zweck                                                          |
|------------------------------|-----------------------------------------------------------------|
| `main.py`                    | FastAPI-App, ein Webhook-Endpoint, dispatcht nach `resource`    |
| `call_session_tracker.py`    | Kernlogik: erkennt verpasste Warteschlangen-Anrufe aus den rohen `telephony_calls`-Events |
| `webex_webhook.py`           | HMAC-Signaturprüfung eingehender Webex-Requests                 |
| `cards.py`                   | Adaptive-Card-JSON für die Webex-Space-Karte                    |
| `webex_bot_client.py`        | Webex-REST-Calls für den Bot/Space-Weg (posten, löschen, DM)     |
| `email_client.py`            | E-Mail-Versand per Microsoft Graph (zwei Betriebsarten, s. unten)|
| `graph_device_login.py`      | Einmaliger Login für die Graph-Betriebsart ohne Admin-Rechte     |
| `store.py`                   | In-Memory-Store mit atomarem "Claim" (s. Produktionshinweise)    |
| `config.py`                  | Settings aus Umgebungsvariablen / `.env`                        |
| `register_webhook.sh`        | Registriert die nötigen Webex-Webhooks                          |
| `_selftest.py`               | Lokaler Test der Erkennungs-/Claim-Logik ohne echtes Netzwerk    |
| `requirements.txt`           | Python-Abhängigkeiten                                            |

## Funktionsweise (kurz)

Webex liefert `telephony_calls`-Events org-weit für **jeden** Anruf, nicht nur
Warteschlangen-Anrufe, und pro Call-Leg (Telefon, App, ...) einzeln. Die
Middleware sammelt alle Events einer `callSessionId` und wertet erst final,
nach einem konfigurierbaren Grace-Fenster ohne neues Event:

- Session wurde beantwortet (`eventType=answered` / `state=connected`
  irgendwann gesehen) → **nie** als verpasst werten.
- Session ist eine ausgehende Verbindung (`personality=originator`) → z. B.
  ein eigener Rückruf, **kein** verpasster eingehender Anruf.
- Kein `redirections`-Eintrag mit `reason=callQueue` gefunden → Direktanruf
  auf eine Nebenstelle, keine Warteschlange, **ignoriert**.
- Sonst, sobald alle bekannten Legs `disconnected` sind: **verpasster Anruf**.

Details und die Herleitung dieser Regeln stehen als Kommentare direkt in
`call_session_tracker.py`.

## Setup

1. **Python 3.12** (neuere Versionen haben teils noch keine fertigen Pakete
   für alle Abhängigkeiten).
2. Abhängigkeiten installieren:
   ```bash
   python -m venv .venv
   .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip.exe
   .venv/bin/pip install msal
   ```
3. `.env.example` nach `.env` kopieren und ausfüllen — jeder Schlüssel ist
   dort kommentiert. Kurzüberblick:
   - `WEBEX_WEBHOOK_SECRET` — beliebiges langes Zufalls-Secret, verifiziert
     eingehende Webex-Requests per HMAC.
   - `WEBEX_BOT_TOKEN` / `WEBEX_SPACE_ID` — **optional**. Nur nötig, wenn die
     Karte in einer Webex-Space gepostet werden soll (braucht eine Webex-
     Messaging-Lizenz). Leer lassen, um ausschließlich per E-Mail zu
     benachrichtigen.
   - `WEBEX_TARGET_QUEUE_NUMBERS` — DNIS-Nummer(n) der Warteschlange(n), auf
     die eingeschränkt werden soll. Leer = jede Warteschlange zählt.
   - `GRAPH_CLIENT_ID` / `GRAPH_TENANT_ID` / `GRAPH_CLIENT_SECRET` / `MAIL_TO`
     — E-Mail-Versand, s. Abschnitt unten.
4. Eine **Service App** bei `developer.webex.com` anlegen (Scope
   `spark-admin:calls_read`) und von einem Full Admin im Webex Control Hub
   unter *Management > Apps > Service Apps* freigeben. Liefert das
   Access Token für die einmalige Webhook-Registrierung (Schritt 6).
5. Middleware starten und von außen erreichbar machen (Tunnel oder Reverse
   Proxy):
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 3978
   ```
6. Webhook registrieren:
   ```bash
   TARGET_URL="https://<eure-öffentliche-adresse>/webhooks/webex" \
   WEBEX_WEBHOOK_SECRET="<gleicher Wert wie in .env>" \
   ./register_webhook.sh
   ```
   Registriert `telephony_calls`/`all` (Anruferkennung, immer) und, falls
   `bot_tokens.env` mit einem `WEBEX_BOT_TOKEN` vorhanden ist, zusätzlich
   `attachmentActions`/`created` (Klick auf "Übernehmen").
7. Testen: echten Anruf in der Warteschlange klingeln lassen und auflegen,
   bevor jemand rangeht. Log zeigt `Verpasster Anruf erkannt: ...`.

## E-Mail-Versand (zwei Betriebsarten)

Automatisch anhand von `GRAPH_CLIENT_SECRET` gewählt, s. Kommentare in
`email_client.py`:

- **Leer** → Delegated Permission + Device Code Login (`graph_device_login.py`
  einmal manuell ausführen). Kein Admin-Consent nötig, aber der Login läuft
  nach 90 Tagen Inaktivität, einem Passwortwechsel oder Conditional-Access-
  Revocation ab und muss dann erneut ausgeführt werden.
- **Gesetzt** → Client Credentials (Application Permission `Mail.Send`).
  Läuft unbeaufsichtigt ohne Ablaufrisiko, braucht aber einmaligen
  Admin-Consent in Entra ID **und** eine Exchange Application Access Policy,
  die den Zugriff der App auf genau ein Postfach einschränkt (sonst dürfte
  die App als jedes Postfach im Tenant senden).

## Bekannte Grenzen

- **In-Memory-Store** (`store.py`): offene Karten/Claims gehen bei einem
  Prozess-Neustart verloren, und mehrere parallele Worker-Prozesse sähen
  nicht denselben Speicher. Für echten Mehrbenutzer-/Hochverfügbarkeits-
  betrieb durch Redis oder eine DB mit atomarem
  `UPDATE ... WHERE claimed_by IS NULL` ersetzen.
- **Erkennungsfenster**: ein Anruf gilt nach `MISSED_CALL_GRACE_SECONDS`
  (Default 8s) Funkstille als verpasst.
- **Eine Instanz**: der atomare Claim-Mechanismus setzt einen einzelnen
  Prozess voraus.
