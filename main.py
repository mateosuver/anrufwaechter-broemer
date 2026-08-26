"""
FastAPI-Middleware: Webex verpasste Anrufe -> interaktive Adaptive Card + E-Mail.

Ein Endpoint:

  POST /webhooks/webex     <- von Webex aufgerufen: sowohl bei jedem
                               telephony_calls-Event (resource=telephony_calls,
                               event=all -- s. call_session_tracker.py, WARUM
                               "jeder Anruf" und nicht nur Warteschlangen-Anrufe)
                               als auch bei jedem Kartenklick
                               (resource=attachmentActions)

Start lokal:
    uvicorn main:app --port 3978

Muss von außen erreichbar sein (z. B. per Tunnel für Tests, produktiv hinter
eurem Reverse Proxy/TLS), damit Webex euch überhaupt erreichen kann.

Die Webex-Karte ist optional: fehlen WEBEX_BOT_TOKEN/WEBEX_SPACE_ID (z. B. weil
der Tenant keine Webex-Messaging-Lizenz hat), wird sie einfach übersprungen und
nur die E-Mail-Benachrichtigung verschickt, s. _post_missed_call_card().
"""
import asyncio
import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from call_session_tracker import CallSessionTracker
from cards import build_claimed_card_webex, build_missed_call_card_webex
from email_client import send_missed_call_email
from config import settings
from store import MissedCallRecord, store
from webex_webhook import verify_signature

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webex-teams-middleware")

app = FastAPI(title="Webex Missed-Call Middleware")


async def _post_missed_call_card(record: dict) -> None:
    """
    Callback für CallSessionTracker: wird genau EINMAL pro callSessionId
    aufgerufen, sobald der Tracker sie final als verpasster Anruf gewertet hat
    (nie beantwortet, alle Legs disconnected, Grace-Fenster verstrichen).
    """
    caller_number = record.get("caller_number")
    if not caller_number:
        logger.warning("Verpasster Anruf ohne caller_number, ignoriert: %s", record)
        return

    call_record = MissedCallRecord(
        call_id=record["call_id"],
        caller_number=caller_number,
        caller_name=record.get("caller_name"),
        queue_name=record.get("queue_name") or "Unbekannte Warteschlange",
        queue_number=record.get("queue_number"),
        timestamp_iso=record.get("timestamp_iso") or "",
    )
    store.add(call_record)
    logger.info(
        "Verpasster Anruf erkannt: call_id=%s caller=%s (%s) queue=%s (%s) time=%s",
        call_record.call_id, call_record.caller_name, call_record.caller_number,
        call_record.queue_name, call_record.queue_number, call_record.timestamp_iso,
    )

    if settings.webex_bot_token and settings.webex_space_id:
        try:
            from webex_bot_client import post_card_to_space

            card = build_missed_call_card_webex(
                call_id=call_record.call_id,
                caller_number=call_record.caller_number,
                caller_name=call_record.caller_name,
                timestamp_iso=call_record.timestamp_iso,
                queue_name=call_record.queue_name,
                queue_number=call_record.queue_number,
            )
            message_id = await post_card_to_space(
                card,
                settings.webex_space_id,
                fallback_text=f"📞 Verpasster Anruf: {call_record.caller_number}",
            )
            store.set_webex_reference(call_record.call_id, settings.webex_space_id, message_id)
            logger.info(
                "Karte für Call %s in Webex-Space gepostet (message_id=%s).",
                call_record.call_id, message_id,
            )
        except Exception:
            logger.exception("Konnte Karte nicht posten (Call %s)", call_record.call_id)
            # 200 an Webex trotzdem zurückgeben (Webhook-Zustellung war ok, das Problem
            # liegt bei uns/Webex) -> hier stattdessen sauber loggen/alerten/retry-queue.
    else:
        # Kein Bot-Token/keine Space konfiguriert -- z. B. weil der Tenant keine Webex-
        # Messaging-Lizenz hat (Chats/Spaces nicht verfügbar). Kein Fehler: die Karte ist
        # dann schlicht nicht nutzbar, nur die E-Mail unten zählt für diesen Kunden.
        logger.info(
            "Webex-Karte übersprungen (WEBEX_BOT_TOKEN/WEBEX_SPACE_ID nicht konfiguriert) "
            "-> nur E-Mail-Versand für Call %s.",
            call_record.call_id,
        )

    # Zusätzlich zur Karte: E-Mail-Benachrichtigung (unabhängig davon, ob die Karte
    # erfolgreich gepostet wurde oder übersprungen ist). send_missed_call_email() ist
    # synchron (Graph-Call per httpx-Sync-Client) -> per to_thread() in einem Worker-
    # Thread, damit der Event-Loop nicht blockiert. Übersprungen (kein Fehler), solange
    # GRAPH_*/MAIL_TO nicht konfiguriert ist, s. email_client.py.
    recipients = _resolve_mail_recipients(record.get("queue_number"))
    try:
        await asyncio.to_thread(
            send_missed_call_email,
            caller_number=call_record.caller_number,
            caller_name=call_record.caller_name,
            timestamp_iso=call_record.timestamp_iso,
            queue_name=call_record.queue_name,
            queue_number=call_record.queue_number,
            recipients=recipients,
        )
    except Exception:
        # send_missed_call_email() fängt Netzwerk-/Graph-Fehler bereits selbst ab -- dieser
        # Fang hier ist nur ein zusätzliches Sicherheitsnetz gegen unerwartete Fehler (z. B.
        # in der Recipient-Auflösung), damit ein Bug im Mailversand niemals die Webhook-
        # Antwort an Webex platzen lässt.
        logger.exception("E-Mail-Versand für Call %s unerwartet fehlgeschlagen.", call_record.call_id)


def _parse_queue_mail_routes(raw: str) -> dict:
    """
    Format: "<queueNumber>=<mail1>,<mail2>;<queueNumber2>=<mail3>", s. config.py.
    Fehlerhafte/unvollständige Gruppen werden übersprungen, nicht hart abgebrochen --
    ein Tippfehler in EINER Route soll nicht den ganzen Mailversand lahmlegen.
    """
    routes: dict = {}
    for group in raw.split(";"):
        group = group.strip()
        if not group or "=" not in group:
            continue
        number, addresses = group.split("=", 1)
        number = number.strip()
        address_list = [addr.strip() for addr in addresses.split(",") if addr.strip()]
        if number and address_list:
            # BUGFIX: ein doppelt angegebener Schlüssel wurde bisher still überschrieben --
            # bei einem Tippfehler/Copy-Paste-Duplikat in QUEUE_MAIL_ROUTES merkt man das
            # sonst nie. Jetzt zumindest sichtbar im Log.
            if number in routes:
                logger.warning(
                    "QUEUE_MAIL_ROUTES: Warteschlangen-Nummer %s mehrfach angegeben, "
                    "letzter Eintrag gewinnt (%s).",
                    number, address_list,
                )
            routes[number] = address_list
    return routes


_queue_mail_routes = _parse_queue_mail_routes(settings.queue_mail_routes)
_default_mail_recipients = [addr.strip() for addr in settings.mail_to.split(",") if addr.strip()]


def _resolve_mail_recipients(queue_number) -> list:
    """QUEUE_MAIL_ROUTES-Treffer für die konkrete Warteschlangen-Nummer, sonst MAIL_TO."""
    if queue_number and queue_number in _queue_mail_routes:
        return _queue_mail_routes[queue_number]
    return _default_mail_recipients


_target_queue_numbers = {
    number.strip() for number in settings.webex_target_queue_numbers.split(",") if number.strip()
}
call_session_tracker = CallSessionTracker(
    on_missed_call=_post_missed_call_card,
    target_queue_numbers=_target_queue_numbers,
    grace_seconds=settings.missed_call_grace_seconds,
)

# Sichtbares Startup-Logging der geparsten Konfiguration -- bei einem Tippfehler in
# .env (z. B. Nummer ohne "+" oder falsches Trennzeichen) sieht man das sonst nur
# indirekt daran, dass nichts (oder das Falsche) passiert.
logger.info(
    "Konfiguration geladen: WEBEX_TARGET_QUEUE_NUMBERS=%s (leer=kein Filter), "
    "QUEUE_MAIL_ROUTES=%s, MAIL_TO(Fallback)=%s, MISSED_CALL_GRACE_SECONDS=%s",
    sorted(_target_queue_numbers) or "(leer)",
    _queue_mail_routes or "(leer)",
    _default_mail_recipients or "(leer)",
    settings.missed_call_grace_seconds,
)

if not settings.webex_webhook_secret:
    # BUGFIX/Härtung: ohne Secret verifiziert verify_signature() GAR NICHTS mehr (s.
    # webex_webhook.py) -> der öffentlich per Tunnel erreichbare Endpoint würde dann
    # JEDEN Request akzeptieren, egal von wem. Für einen kurzen lokalen Test tragbar,
    # für einen dauerhaft laufenden Server nicht -- deshalb laut im Log, nicht nur
    # eine stille Zeile in webex_webhook.py.
    logger.warning(
        "WEBEX_WEBHOOK_SECRET ist leer -> Signaturprüfung für /webhooks/webex ist "
        "DEAKTIVIERT. Der öffentlich erreichbare Endpoint akzeptiert dann JEDEN Request "
        "ohne Prüfung, ob er wirklich von Webex kommt. Für einen dauerhaft laufenden "
        "Server unbedingt ein Secret in .env setzen!"
    )


# ---------------------------------------------------------------------------
# Webex-Webhook-Empfang (telephony_calls UND Kartenklick landen hier,
# unterschieden über payload["resource"])
# ---------------------------------------------------------------------------
@app.post("/webhooks/webex")
async def webex_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Spark-Signature")

    if not verify_signature(raw_body, signature):
        logger.warning("Webex-Webhook: ungültige Signatur, Request verworfen.")
        return JSONResponse(status_code=401, content={"error": "invalid signature"})

    payload = await request.json()
    resource = payload.get("resource")
    # Kompletter, ungefilterter Envelope für JEDES Event (nicht nur bestimmte
    # eventTypes) -- explizit wegen des Falsch-Alarm-Bugs: wir brauchen jetzt
    # auch created/updated-Events (answered/connected), um Warteschlangen-
    # Anrufe von normalen Anrufen unterscheiden zu können, s.
    # call_session_tracker.py.
    logger.info(
        "Webex-Webhook empfangen: resource=%s event=%s actorId=%s data=%s",
        resource, payload.get("event"), payload.get("actorId"), payload.get("data"),
    )

    if resource == "attachmentActions":
        return await _handle_webex_card_click(payload)

    if resource == "telephony_calls":
        call_session_tracker.record_event(payload.get("data", {}))
        return Response(status_code=200)

    # Unbekannte/andere Resource -> ignorieren, aber 200 zurückgeben, sonst
    # deaktiviert Webex den Webhook nach zu vielen Fehlern.
    return Response(status_code=200)


async def _handle_webex_card_click(payload: dict) -> Response:
    """
    Reaktion auf einen Klick in der Webex-Space-Karte (Action.Submit -> Webex
    feuert ein "attachmentActions"/"created"-Event). Der Webhook liefert NUR die
    action_id, die eigentlichen Eingabedaten (call_id) müssen separat per GET
    nachgeladen werden (s. webex_bot_client.get_attachment_action).
    """
    from webex_bot_client import (
        delete_message,
        get_attachment_action,
        get_person_display_name,
        post_card_to_space,
        send_direct_message,
    )

    data = payload.get("data", {})
    action_id = data.get("id")
    if not action_id:
        return Response(status_code=200)

    action = await get_attachment_action(action_id)
    inputs = action.get("inputs", {}) or {}
    call_id = inputs.get("call_id")
    person_id = action.get("personId")
    room_id = action.get("roomId")

    if not call_id:
        logger.warning("attachmentActions-Event ohne call_id, ignoriert: %s", action_id)
        return Response(status_code=200)

    agent_name = await get_person_display_name(person_id) if person_id else "Unbekannter Agent"
    won, record = store.claim_call(call_id, agent_name)

    if record is None:
        logger.warning("Kartenklick für unbekannten call_id=%s (Server evtl. neu gestartet).", call_id)
        return Response(status_code=200)

    if won:
        # Alte, offene Karte löschen + neue grüne Bestätigung an derselben Stelle posten
        # (Ersatz für "in-place Update", s. Docstring in webex_bot_client.py).
        if record.webex_message_id:
            await delete_message(record.webex_message_id)
        # Als richtige Adaptive Card posten (nicht als rohe Markdown-Tabelle), damit
        # die Bestätigung optisch wie die ursprüngliche Karte wirkt -- mit
        # eingebettetem tel:-Rückruf-Link im Card-Body statt separatem Text, s.
        # Docstring von build_claimed_card_webex().
        confirm_card = build_claimed_card_webex(
            caller_number=record.caller_number,
            caller_name=record.caller_name,
            timestamp_iso=record.timestamp_iso,
            queue_name=record.queue_name,
            queue_number=record.queue_number,
            claimed_by=agent_name,
        )
        await post_card_to_space(
            confirm_card,
            room_id,
            fallback_text=f"✅ Übernommen von {agent_name}: {record.caller_number}",
        )
        logger.info("Call %s von %s übernommen.", call_id, agent_name)
    else:
        # Ein anderer Agent war schneller -> nur DIESER Klickende bekommt per 1:1-
        # Nachricht Bescheid (Webex kennt keine "nur für mich sichtbare" Invoke-Antwort).
        if person_id:
            await send_direct_message(
                person_id,
                f"Schon von **{record.claimed_by or 'jemand anderem'}** übernommen – kein Doppel-Rückruf nötig.",
            )
        logger.info("Call %s bereits von %s übernommen, %s kam zu spät.", call_id, record.claimed_by, agent_name)

    return Response(status_code=200)
