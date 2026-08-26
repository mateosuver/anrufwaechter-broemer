#!/usr/bin/env bash
# Registriert die für die Anruferkennung nötigen Webhooks:
#   1. "telephony_calls" / "all"          -> alle Call-Lifecycle-Events (verpasster
#                                             Anruf wird serverseitig in main.py über
#                                             mehrere Events pro callSessionId erkannt,
#                                             s. Kommentare dort -- ein einzelnes
#                                             "deleted"-Event allein reichte nicht,
#                                             weil uns die "answered"-Events fehlten)
#   2. "attachmentActions" / "created"    -> Klick auf "Übernehmen" in der Karte
#      NUR wenn bot_tokens.env vorhanden ist / WEBEX_BOT_TOKEN gesetzt ist. Ohne
#      Webex-Messaging-Lizenz (kein Bot, keine Space nutzbar, z. B. Brömer & Sohn)
#      wird dieser zweite Webhook übersprungen -- die Middleware verschickt in dem
#      Fall ohnehin nur noch die E-Mail, kein Kartenklick zum Abfangen nötig.
# Beide zeigen auf denselben Endpoint (main.py unterscheidet selbst über
# payload["resource"]).
#
# HINWEIS: "telephony_queue" (wie ursprünglich angenommen) existiert nicht als
# Webhook-Resource -> Webex API antwortet mit 400 "invalid field: resource".
# Die tatsächliche Resource heißt "telephony_calls" und braucht zwingend
# "ownedBy": "org", sonst 403 "Not allowed to create webhook" (verifiziert
# 21.08.2026). Es gibt KEIN separates "telephony_queue"-Event für Warteschlangen
# -- telephony_calls feuert für JEDEN Anruf org-weit, auch für normale
# ausgehende Rückrufe (verifiziert per echtem Testanruf) -> main.py muss
# Warteschlangen-Anrufe von normalen Anrufen unterscheiden, s. Kommentare dort.
#
# Voraussetzungen:
#   1. service_app_tokens.env liegt in diesem Ordner (Access Token der Service App,
#      Scope spark-admin:calls_read -> für telephony_calls). IMMER nötig.
#   2. bot_tokens.env liegt in diesem Ordner (Bot-Token -> für attachmentActions;
#      ein Bot darf Webhooks für seine EIGENEN Nachrichten/Räume registrieren,
#      braucht dafür keine Admin-Scopes). NUR nötig, wenn ihr die Webex-Karte
#      überhaupt nutzt (Messaging-Lizenz vorhanden).
#   3. Die Middleware läuft (uvicorn) und ist unter TARGET_URL von außen erreichbar.
#   4. WEBEX_WEBHOOK_SECRET ist gesetzt (identisch zu eurer .env für die Middleware
#      selbst, sonst schlägt die HMAC-Signaturprüfung in main.py fehl).
#
# Aufruf:
#   TARGET_URL="https://<eure-tunnel-oder-domain>/webhooks/webex" \
#   WEBEX_WEBHOOK_SECRET="<gleicher Wert wie in eurer .env>" \
#   ./register_webhook.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for f in service_app_tokens.env bot_tokens.env; do
  if [ -f "$SCRIPT_DIR/$f" ]; then
    # shellcheck disable=SC1090
    source "$SCRIPT_DIR/$f"
  fi
done

: "${WEBEX_SERVICE_APP_ACCESS_TOKEN:?Fehlt: WEBEX_SERVICE_APP_ACCESS_TOKEN (siehe service_app_tokens.env)}"
: "${TARGET_URL:?Fehlt: TARGET_URL, z. B. TARGET_URL=https://xxxx.trycloudflare.com/webhooks/webex}"
: "${WEBEX_WEBHOOK_SECRET:?Fehlt: WEBEX_WEBHOOK_SECRET (muss mit dem Wert in eurer .env übereinstimmen)}"

echo "1/2: Registriere 'telephony_calls' (verpasster Anruf) -> $TARGET_URL ..."
curl -sS -L --request POST \
  --url 'https://webexapis.com/v1/webhooks' \
  --header "Authorization: Bearer ${WEBEX_SERVICE_APP_ACCESS_TOKEN}" \
  --header 'Content-Type: application/json' \
  --data "{
    \"name\": \"Verpasste Anrufe Warteschlange\",
    \"targetUrl\": \"${TARGET_URL}\",
    \"resource\": \"telephony_calls\",
    \"event\": \"all\",
    \"ownedBy\": \"org\",
    \"secret\": \"${WEBEX_WEBHOOK_SECRET}\"
  }" | tee /tmp/webhook_registration_missed_call.json
echo

if [ -n "${WEBEX_BOT_TOKEN:-}" ]; then
  echo "2/2: Registriere 'attachmentActions' (Kartenklick) -> $TARGET_URL ..."
  curl -sS -L --request POST \
    --url 'https://webexapis.com/v1/webhooks' \
    --header "Authorization: Bearer ${WEBEX_BOT_TOKEN}" \
    --header 'Content-Type: application/json' \
    --data "{
      \"name\": \"Uebernehmen-Klick auf Missed-Call-Karte\",
      \"targetUrl\": \"${TARGET_URL}\",
      \"resource\": \"attachmentActions\",
      \"event\": \"created\",
      \"secret\": \"${WEBEX_WEBHOOK_SECRET}\"
    }" | tee /tmp/webhook_registration_attachment_action.json
  echo
else
  echo "2/2: WEBEX_BOT_TOKEN nicht gesetzt -> 'attachmentActions'-Webhook übersprungen"
  echo "(keine Webex-Karte konfiguriert, z. B. mangels Messaging-Lizenz -- nur E-Mail-Versand aktiv)."
fi

echo
echo "Antwort(en) oben. Bei Erfolg jeweils Feld \"id\" + \"status\": \"active\"."
echo
echo "Jetzt testen: echten Anruf in der Warteschlange auflegen lassen, bevor jemand"
echo "rangeht. In den Logs der laufenden Middleware sollte kurz danach"
echo "\"Verpasster Anruf erkannt: ...\" erscheinen, und die E-Mail (und falls"
echo "konfiguriert die Karte) ankommen."
