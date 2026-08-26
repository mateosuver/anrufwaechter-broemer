"""
Zentrale Konfiguration – alles aus Umgebungsvariablen (.env für lokale Entwicklung).
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # --- Webex ---
    webex_webhook_secret: str = os.getenv("WEBEX_WEBHOOK_SECRET", "")

    # --- Webex Bot + Space -- BEIDE optional: leer gelassen (z. B. mangels Webex-
    # Messaging-Lizenz), wird die Karte einfach übersprungen und nur die E-Mail
    # (s. unten) verschickt, s. main.py._post_missed_call_card.
    webex_bot_token: str = os.getenv("WEBEX_BOT_TOKEN", "")
    webex_bot_email: str = os.getenv("WEBEX_BOT_EMAIL", "")
    # roomId der Space, in die gepostet werden soll. Bot muss dort vorher als Mitglied
    # hinzugefügt worden sein (s. bot_tokens.env), sonst schlägt das Posten fehl.
    webex_space_id: str = os.getenv("WEBEX_SPACE_ID", "")

    # Komma-getrennte Liste von Warteschlangen-Nummern (DNIS), auf die verpasste
    # Anrufe eingeschränkt werden sollen, z. B. "+4989248815150,+4989248815199".
    # Leer (Default) = KEIN Filter, jeder nie beantwortete eingehende Anruf zählt,
    # auch Direktanrufe ohne Warteschlange. Die Nummer steht im telephony_calls-
    # Event unter data.redirections[].redirectingParty.number (nur vorhanden, wenn
    # reason=="callQueue"), s. call_session_tracker.py.
    webex_target_queue_numbers: str = os.getenv("WEBEX_TARGET_QUEUE_NUMBERS", "")

    # Sekunden Funkstille, nach denen eine nie beantwortete, komplett disconnectete
    # Call-Session final als "verpasst" gewertet wird (s. call_session_tracker.py).
    # War bisher als Konstante im Code fest verdrahtet (Bug-Hunt 26.08.2026) --
    # jetzt konfigurierbar, falls ein Tenant z. B. mehr Legs mit größeren zeitlichen
    # Abständen zwischen den Events hat.
    missed_call_grace_seconds: float = float(os.getenv("MISSED_CALL_GRACE_SECONDS", "8"))

    # --- E-Mail-Benachrichtigung bei verpasstem Anruf (zusätzlich zur Webex-Karte) ---
    # Per Microsoft Graph, s. email_client.py. ZWEI Betriebsarten, automatisch anhand
    # von GRAPH_CLIENT_SECRET gewählt:
    #
    #   1. GRAPH_CLIENT_SECRET gesetzt -> Client Credentials (Application Permission
    #      "Mail.Send", einmaliger Admin-Consent + Exchange Application Access Policy
    #      nötig, s. graphsendmailsetup.md). Läuft danach völlig unbeaufsichtigt, KEIN
    #      Ablaufrisiko wie bei Variante 2. Sendet über POST /users/{MAIL_FROM}/sendMail
    #      (kein "/me" im App-Only-Kontext, MAIL_FROM bestimmt den Absender/das Postfach).
    #
    #   2. GRAPH_CLIENT_SECRET leer -> Delegated Permission + Device Code Login (s.
    #      graph_device_login.py), gedacht für Accounts OHNE Admin-Rechte im Tenant.
    #      Sendet über POST /me/sendMail (Absender = wer sich im Device-Code-Flow
    #      angemeldet hat). ACHTUNG bekannte Einschränkung: der Refresh Token in
    #      graph_token_cache_path gehört dem angemeldeten Benutzer, nicht der App --
    #      stirbt nach 90 Tagen Inaktivität, bei Passwortwechsel oder Conditional-
    #      Access-Session-Revocation. Dann muss graph_device_login.py manuell erneut
    #      ausgeführt werden (kein Fehler-Alarm, E-Mail-Versand wird in email_client.py
    #      einfach übersprungen und geloggt).
    graph_client_id: str = os.getenv("GRAPH_CLIENT_ID", "")
    graph_tenant_id: str = os.getenv("GRAPH_TENANT_ID", "")
    graph_client_secret: str = os.getenv("GRAPH_CLIENT_SECRET", "")
    graph_token_cache_path: str = os.getenv("GRAPH_TOKEN_CACHE_PATH", "graph_token_cache.json")
    # Absender/Postfach. Bei Client Credentials (Variante 1) bestimmt DAS, wer sendet
    # (die App-Registrierung braucht per Exchange Application Access Policy Zugriff
    # genau auf dieses Postfach). Bei Device Code (Variante 2) MUSS es derselbe Account
    # sein, der sich im Device-Code-Flow angemeldet hat -- /me/sendMail sendet immer
    # als der angemeldete User, MAIL_FROM ist dort nur der Anzeigewert im Mail-Header.
    mail_from: str = os.getenv("MAIL_FROM", "")
    # Fallback-Empfänger (komma-getrennt), falls für die jeweilige Warteschlange KEIN
    # Eintrag in QUEUE_MAIL_ROUTES existiert (oder der Anruf keiner Warteschlange
    # zuordenbar war) -- z. B. "office@example.com".
    mail_to: str = os.getenv("MAIL_TO", "")
    # Mehrere Warteschlangen -> verschiedene Postfächer: Format
    # "<queueNumber>=<mail1>,<mail2>;<queueNumber2>=<mail3>", z. B.
    # "+4989248815150=office@example.com;+4989248815199=support@example.com"
    # Leer (Default) = alle verpassten Anrufe gehen an MAIL_TO, unabhängig von der
    # Warteschlange. Die Nummer muss exakt der DNIS aus data.redirections entsprechen
    # (s. call_session_tracker.py) -- Groß-/Kleinschreibung und Leerzeichen egal,
    # Formatierung der Nummer selbst (z. B. mit/ohne "+") muss exakt matchen.
    queue_mail_routes: str = os.getenv("QUEUE_MAIL_ROUTES", "")


settings = Settings()
