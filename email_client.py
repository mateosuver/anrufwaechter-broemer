"""
E-Mail-Benachrichtigung bei verpasstem Anruf, per Microsoft Graph (`POST .../sendMail`).

Zusätzlich zur Webex-Karte, nicht als Ersatz -- wird aus main.py._post_missed_call_card
aufgerufen, nachdem (oder unabhängig davon, ob) die Karte gepostet wurde.

##########################################################################
# ZWEI BETRIEBSARTEN, automatisch anhand von GRAPH_CLIENT_SECRET gewählt  #
##########################################################################

1. GRAPH_CLIENT_SECRET gesetzt -> CLIENT CREDENTIALS (Application Permission
   "Mail.Send"). Läuft völlig unbeaufsichtigt, kein Ablaufrisiko -- die
   "richtige" Variante für Dauerbetrieb, braucht aber einmalig:
     - Admin-Consent für die Application Permission "Mail.Send"
     - eine Exchange Application Access Policy, die den Zugriff der App auf
       GENAU das Postfach MAIL_FROM einschränkt (sonst könnte die App als
       JEDER im Tenant senden)
   Komplette Schritt-für-Schritt-Anleitung: graphsendmailsetup.md. Sendet über
   POST /v1.0/users/{MAIL_FROM}/sendMail (kein "/me" im App-Only-Kontext).

2. GRAPH_CLIENT_SECRET leer -> DELEGATED PERMISSION + DEVICE CODE FLOW (s.
   graph_device_login.py, einmal manuell ausführen). Gedacht für Accounts
   OHNE Admin-Rechte im Tenant (kein Admin-Consent nötig für Mail.Send als
   Delegated Permission). Sendet über POST /v1.0/me/sendMail (Absender = wer
   sich im Device-Code-Flow angemeldet hat). ACHTUNG bekannte Einschränkung:
   der Refresh Token in graph_token_cache_path gehört dem angemeldeten
   Benutzer, nicht der App -- stirbt nach 90 Tagen Inaktivität, bei Passwort-
   wechsel oder Conditional-Access-Session-Revocation. Dann muss
   graph_device_login.py manuell erneut ausgeführt werden. Diese Datei hier
   loggt in dem Fall nur eine Warnung, wirft aber keinen Fehler, der die
   Webex-Karte mitreißen würde.
"""
import html
import logging
import threading
import time
from typing import Optional

import httpx
import msal

from config import settings

logger = logging.getLogger("webex-teams-middleware.email_client")

# "offline_access" NICHT hier auflisten (Device-Code-Pfad), s. Kommentar in
# graph_device_login.py.
DELEGATED_SCOPES = ["Mail.Send"]
CLIENT_CREDENTIALS_SCOPES = ["https://graph.microsoft.com/.default"]
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# BUGFIX (Bug-Hunt 26.08.2026): send_missed_call_email() läuft über asyncio.to_thread()
# in einem eigenen Worker-Thread pro Aufruf -- bei zwei fast gleichzeitigen verpassten
# Anrufen könnten zwei Threads parallel dieselbe Token-Cache-Datei/den In-Memory-Cache
# lesen/erneuern/überschreiben. Lock serialisiert das (billig, verpasste Anrufe sind
# kein Hot-Path).
_token_lock = threading.Lock()

# In-Memory-Cache NUR für den Client-Credentials-Pfad (Variante 1) -- MSAL cacht dort
# nichts auf Platte, s. graphsendmailsetup.md ("Token cachen, nicht pro Mail neu holen").
_cc_token_cache: dict = {"access_token": "", "expires_at": 0.0}


def _use_client_credentials() -> bool:
    return bool(settings.graph_client_secret)


def _is_configured() -> bool:
    if _use_client_credentials():
        return bool(settings.graph_client_id and settings.graph_tenant_id and settings.mail_from)
    return bool(settings.graph_client_id and settings.graph_tenant_id)


def _send_mail_url() -> str:
    if _use_client_credentials():
        # App-Only-Token kennt kein "/me" -> Postfach explizit über den UPN ansprechen.
        return f"{GRAPH_BASE_URL}/users/{settings.mail_from}/sendMail"
    return f"{GRAPH_BASE_URL}/me/sendMail"


def _acquire_token_client_credentials() -> Optional[str]:
    with _token_lock:
        now = time.time()
        if _cc_token_cache["access_token"] and now < _cc_token_cache["expires_at"] - 60:
            return _cc_token_cache["access_token"]

        app = msal.ConfidentialClientApplication(
            settings.graph_client_id,
            authority=f"https://login.microsoftonline.com/{settings.graph_tenant_id}",
            client_credential=settings.graph_client_secret,
        )
        result = app.acquire_token_for_client(scopes=CLIENT_CREDENTIALS_SCOPES)

        if not result or "access_token" not in result:
            error = (result or {}).get("error_description", "unbekannter Fehler")
            logger.warning(
                "Graph-Token (Client Credentials) konnte nicht geholt werden (%s) -- "
                "GRAPH_CLIENT_SECRET/GRAPH_CLIENT_ID/GRAPH_TENANT_ID prüfen, oder ist der "
                "Admin-Consent für 'Mail.Send' (Application Permission) noch nicht erteilt?",
                error,
            )
            return None

        _cc_token_cache["access_token"] = result["access_token"]
        _cc_token_cache["expires_at"] = now + result.get("expires_in", 3600)
        return result["access_token"]


def _acquire_token_device_code() -> Optional[str]:
    with _token_lock:
        cache = msal.SerializableTokenCache()
        try:
            with open(settings.graph_token_cache_path, "r", encoding="utf-8") as f:
                cache.deserialize(f.read())
        except FileNotFoundError:
            logger.warning(
                "Kein Graph-Token-Cache unter %s gefunden -> bitte einmalig "
                "'graph_device_login.py' ausführen.",
                settings.graph_token_cache_path,
            )
            return None

        app = msal.PublicClientApplication(
            settings.graph_client_id,
            authority=f"https://login.microsoftonline.com/{settings.graph_tenant_id}",
            token_cache=cache,
        )
        accounts = app.get_accounts()
        if not accounts:
            logger.warning(
                "Graph-Token-Cache leer/ungültig -> bitte 'graph_device_login.py' erneut ausführen."
            )
            return None

        result = app.acquire_token_silent(DELEGATED_SCOPES, account=accounts[0])

        # acquire_token_silent erneuert den Cache-Inhalt (neuer Access Token) -> zurückschreiben,
        # damit der nächste Aufruf nicht unnötig einen weiteren Refresh macht.
        if cache.has_state_changed:
            with open(settings.graph_token_cache_path, "w", encoding="utf-8") as f:
                f.write(cache.serialize())

        if not result or "access_token" not in result:
            error = (result or {}).get("error_description", "unbekannter Fehler")
            logger.warning(
                "Graph-Token konnte nicht (mehr) erneuert werden (%s) -> Refresh Token vermutlich "
                "abgelaufen/widerrufen. Bitte 'graph_device_login.py' erneut ausführen.",
                error,
            )
            return None

        return result["access_token"]


def _acquire_token() -> Optional[str]:
    if _use_client_credentials():
        return _acquire_token_client_credentials()
    return _acquire_token_device_code()


def send_missed_call_email(
    caller_number: str,
    caller_name: Optional[str],
    timestamp_iso: str,
    queue_name: str,
    queue_number: Optional[str] = None,
    recipients: Optional[list] = None,
) -> None:
    """
    Synchron/blockierend (msal + httpx-Sync-Client) -- wird deshalb vom Aufrufer
    über asyncio.to_thread() in einem Worker-Thread ausgeführt, damit der
    FastAPI-Event-Loop währenddessen nicht blockiert.

    `recipients` wird normalerweise von main.py übergeben (Ergebnis von
    _resolve_mail_recipients(), das QUEUE_MAIL_ROUTES gegen MAIL_TO auflöst).
    None -> fällt direkt auf MAIL_TO zurück (z. B. für manuelle/Test-Aufrufe).
    """
    if recipients is None:
        recipients = [addr.strip() for addr in settings.mail_to.split(",") if addr.strip()]

    if not _is_configured():
        logger.info(
            "E-Mail-Versand nicht konfiguriert (GRAPH_CLIENT_ID/GRAPH_TENANT_ID%s "
            "fehlt in .env) -> übersprungen.",
            "/MAIL_FROM" if _use_client_credentials() else "",
        )
        return

    if not recipients:
        logger.info("E-Mail-Versand: keine Empfänger ermittelt (MAIL_TO/QUEUE_MAIL_ROUTES leer) -> übersprungen.")
        return

    access_token = _acquire_token()
    if not access_token:
        return  # Warnung wurde bereits in _acquire_token_*() geloggt

    caller_display = caller_name or caller_number

    # BUGFIX (Bug-Hunt 26.08.2026): caller_name/caller_number kommen letztlich vom
    # Anrufer (Caller-ID-Name ist theoretisch spoofbar) und wurden bisher UNESCAPED
    # in einen HTML-Mailbody eingebettet -- html.escape() schließt dieses (schmale,
    # aber echte) HTML-Injection-Fenster. Bei den Adaptive-Card-Feldern (cards.py)
    # nicht nötig: FactSet-Werte werden dort als reiner Text gerendert, kein HTML.
    caller_display_safe = html.escape(caller_display)
    caller_number_safe = html.escape(caller_number)
    timestamp_safe = html.escape(timestamp_iso)
    queue_name_safe = html.escape(queue_name)
    queue_number_safe = html.escape(queue_number) if queue_number else "unbekannt"

    html_body = f"""
    <p><strong>📞 Verpasster Anruf in Warteschlange</strong></p>
    <table border="1" cellpadding="4" cellspacing="0">
      <tr><td>Anrufer</td><td>{caller_display_safe}</td></tr>
      <tr><td>Nummer</td><td>{caller_number_safe}</td></tr>
      <tr><td>Uhrzeit</td><td>{timestamp_safe}</td></tr>
      <tr><td>Warteschlange</td><td>{queue_name_safe}</td></tr>
      <tr><td>Angerufene Nummer</td><td>{queue_number_safe}</td></tr>
    </table>
    <p>\U0001F4F2 <a href="tel:{caller_number_safe}">Jetzt zurückrufen: {caller_number_safe}</a></p>
    """

    payload = {
        "message": {
            "subject": f"Verpasster Anruf: {caller_display}",
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in recipients],
        },
        "saveToSentItems": True,
    }

    try:
        resp = httpx.post(
            _send_mail_url(),
            headers={"Authorization": f"Bearer {access_token}"},
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()  # Erfolg = 202 Accepted, leerer Body
        logger.info("E-Mail für verpassten Anruf an %s verschickt.", recipients)
    except Exception:
        logger.exception("Konnte E-Mail für verpassten Anruf nicht verschicken.")
