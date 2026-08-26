"""
Baut die Adaptive Cards für den Webex-Space-Weg (verpasster Anruf + Bestätigung).
"""
from typing import Optional


def build_missed_call_card_webex(
    call_id: str,
    caller_number: str,
    caller_name: Optional[str],
    timestamp_iso: str,
    queue_name: str,
    queue_number: Optional[str] = None,
) -> dict:
    """
    ACHTUNG version="1.3": Webex' /v1/messages-Endpoint lehnt "1.4" mit 400
    "Adaptive Card version is not yet supported" ab (verifiziert per echtem
    API-Call, 21.08.2026) -- unterstützt aktuell nur bis 1.3.

    ACHTUNG Rückruf-Button: Webex' Action.OpenUrl lehnt das "tel:"-Schema ab
    (400 "Invalid URL provided for Action.OpenUrl", verifiziert per echtem
    API-Call). Deshalb hier kein Rückruf-Button -- die Nummer steht bereits
    im FactSet für manuelles Wählen.
    """
    caller_display = caller_name or caller_number

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.3",
        "body": [
            {
                "type": "Container",
                "style": "attention",
                "bleed": True,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "📞 Verpasster Anruf in Warteschlange",
                        "weight": "Bolder",
                        "size": "Medium",
                        "wrap": True,
                    }
                ],
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Anrufer", "value": caller_display},
                    {"title": "Nummer", "value": caller_number},
                    {"title": "Uhrzeit", "value": timestamp_iso},
                    {"title": "Warteschlange", "value": queue_name},
                    {"title": "Angerufene Nummer", "value": queue_number or "unbekannt"},
                ],
            },
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "✋ Übernehmen",
                "data": {"call_id": call_id},
            },
        ],
    }


def build_claimed_card_webex(
    caller_number: str,
    caller_name: Optional[str],
    timestamp_iso: str,
    queue_name: str,
    claimed_by: str,
    queue_number: Optional[str] = None,
) -> dict:
    """
    Bestätigungskarte, gepostet anstelle der gelöschten Missed-Call-Karte (s.
    Docstring in webex_bot_client.py zum "kein In-Place-Update"-Workaround).
    Sieht bewusst wie eine "richtige" Karte aus (gleicher Aufbau wie
    build_missed_call_card_webex), nicht wie eine rohe Markdown-Nachricht.

    version="1.3" aus demselben Grund wie in build_missed_call_card_webex.
    Der Rückruf-Link steckt hier als simpler Markdown-Link IM Card-Body
    (TextBlock), NICHT als Action.OpenUrl -- Webex lehnt Action.OpenUrl mit
    "tel:"-Schema hart ab, aber unvalidierten Markdown-Text in einem TextBlock
    lässt es durch. Ob der jeweilige Webex-Client daraus einen klickbaren Link
    macht, hängt vom Client ab.
    """
    caller_display = caller_name or caller_number

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.3",
        "body": [
            {
                "type": "Container",
                "style": "good",
                "bleed": True,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": f"✅ Übernommen von {claimed_by}",
                        "weight": "Bolder",
                        "size": "Medium",
                        "wrap": True,
                    }
                ],
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Anrufer", "value": caller_display},
                    {"title": "Nummer", "value": caller_number},
                    {"title": "Uhrzeit", "value": timestamp_iso},
                    {"title": "Warteschlange", "value": queue_name},
                    {"title": "Angerufene Nummer", "value": queue_number or "unbekannt"},
                ],
            },
            {
                "type": "TextBlock",
                "text": f"📲 [Jetzt zurückrufen: {caller_number}](tel:{caller_number})",
                "wrap": True,
            },
        ],
    }
