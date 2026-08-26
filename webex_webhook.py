"""
Alles rund um den eingehenden Webex-Webhook: Signaturprüfung.

##########################################################################
# STAND NACH ECHTEN TESTANRUFEN (21.08.2026)                              #
##########################################################################

Frühere Annahmen hier waren FALSCH und wurden durch echte Testanrufe
widerlegt:

  - resource "telephony_queue" existiert nicht -> Webex API antwortet mit
    400 "invalid field: resource". Die tatsächliche Resource heißt
    "telephony_calls" (braucht "ownedBy": "org" bei der Registrierung, s.
    register_webhook.sh) und feuert org-weit für JEDEN Anruf, nicht nur für
    Warteschlangen-Anrufe.

  - Ein einzelnes "deleted"-Event ohne `data.answered` reicht NICHT als
    Missed-Call-Indikator: ein eigener, nicht angenommener Rückruf sieht
    genauso aus wie ein in der Warteschlange verlorener Anruf. Außerdem
    verpasst man mit event="deleted" alle "answered"/"connected"-Events, die
    zur korrekten Beurteilung nötig sind.

Die eigentliche Auswertungslogik (Events pro callSessionId sammeln, auf
"answered"/"connected" prüfen, Grace-Fenster abwarten) lebt deshalb jetzt in
`call_session_tracker.py` -- dieses Modul hier kümmert sich nur noch um die
Signaturprüfung der eingehenden Requests.
"""
import hashlib
import hmac
from typing import Optional

from config import settings


def verify_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """
    Webex signiert Webhook-Requests per HMAC-SHA1 über den rohen Request-Body,
    mit dem Secret, das ihr beim Anlegen des Webhooks angegeben habt.
    Header: "X-Spark-Signature".
    """
    if not settings.webex_webhook_secret:
        # Kein Secret konfiguriert -> Prüfung übersprungen (nur für lokale Tests!).
        return True
    if not signature_header:
        return False

    expected = hmac.new(
        settings.webex_webhook_secret.encode("utf-8"),
        raw_body,
        hashlib.sha1,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
