"""
Alle Webex-eigenen REST-Calls für den Bot-Weg (Karte in einer Webex-Space).

Läuft nativ über eine Webex Space + einen Webex-Bot -- kein Azure, keine Bot-
Framework-Registrierung nötig, ein Bot ist in ~30 Sekunden über
developer.webex.com angelegt. Optional: fehlt eine Webex-Messaging-Lizenz
(kein Bot/keine Space nutzbar), überspringt main.py diesen Teil einfach und
verschickt nur die E-Mail-Benachrichtigung (s. email_client.py).

WICHTIG, Unterschied zur ursprünglichen Spezifikation (Karte "in-place" updaten):
Laut Webex-API-Referenz (Edit a Message, developer.webex.com/docs/api/v1/
messages/edit-a-message): "Edits of messages containing files or attachments
are not currently supported." -> eine gepostete Adaptive Card kann NICHT per
PUT nachträglich geändert werden (anders als bei Teams über Action.Execute).

Deshalb hier der Workaround, der optisch auf dasselbe hinausläuft:
  1. Beim Klick auf "Übernehmen" wird die ursprüngliche Karten-Nachricht
     GELÖSCHT (der Bot darf eigene Nachrichten immer löschen).
  2. Direkt danach postet der Bot eine NEUE, grüne Bestätigungsnachricht
     ("✅ Übernommen von ...") in dieselbe Space.
Für alle Space-Mitglieder sieht das im Chat-Verlauf praktisch identisch aus
zu einem In-Place-Update (Karte verschwindet, Status-Meldung erscheint an
"derselben Stelle" im Gesprächsfluss) -- nur eben zwei Nachrichten statt eine
editierte.
"""
import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger("webex-teams-middleware.webex_bot")

BASE_URL = "https://webexapis.com/v1"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.webex_bot_token}",
        "Content-Type": "application/json",
    }


async def post_card_to_space(card: dict, room_id: str, fallback_text: str = "Neue Karte") -> str:
    """Postet eine neue Adaptive Card in die Space. Gibt die message_id zurück."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/messages",
            headers=_headers(),
            json={
                "roomId": room_id,
                "markdown": fallback_text,
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": card,
                    }
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["id"]


async def post_plain_message(room_id: str, markdown: str) -> str:
    """Postet eine einfache Text-/Markdown-Nachricht (z. B. die grüne 'Übernommen'-Bestätigung)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/messages",
            headers=_headers(),
            json={"roomId": room_id, "markdown": markdown},
        )
        resp.raise_for_status()
        return resp.json()["id"]


async def delete_message(message_id: str) -> None:
    """Löscht eine vom Bot selbst gepostete Nachricht (z. B. die alte, offene Karte)."""
    async with httpx.AsyncClient() as client:
        resp = await client.delete(f"{BASE_URL}/messages/{message_id}", headers=_headers())
        # 404 = Nachricht war schon weg (z. B. doppelter Klick) -> kein Fehler, einfach ignorieren.
        if resp.status_code not in (204, 404):
            resp.raise_for_status()


async def get_attachment_action(action_id: str) -> dict:
    """
    Holt die Details + Eingabedaten eines Button-Klicks.
    (Webhook liefert nur die action_id, nicht die eigentlichen `inputs` -> zweiter Call nötig,
    exakt wie im developer.webex.com-Beispiel für "Get Attachment Action Details" dokumentiert.)
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/attachment/actions/{action_id}", headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def get_person_display_name(person_id: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/people/{person_id}", headers=_headers())
        resp.raise_for_status()
        return resp.json().get("displayName", "Unbekannter Agent")


async def send_direct_message(person_id: str, markdown: str) -> None:
    """Für den Agenten, der zu spät geklickt hat -> kurze persönliche 1:1-Nachricht vom Bot."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/messages",
            headers=_headers(),
            json={"toPersonId": person_id, "markdown": markdown},
        )
        resp.raise_for_status()


async def list_rooms(bot_token: Optional[str] = None) -> list[dict]:
    """
    Hilfsfunktion NUR fürs einmalige Setup: listet alle Spaces, in denen der Bot
    bereits Mitglied ist, damit man die roomId nicht manuell aus der URL fummeln muss.
    (Space in Webex öffnen -> Bot per E-Mail hinzufügen -> dieses Skript/Endpoint
    zeigt dann die passende roomId.)
    """
    headers = {"Authorization": f"Bearer {bot_token or settings.webex_bot_token}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/rooms", headers=headers, params={"max": 50})
        resp.raise_for_status()
        return resp.json().get("items", [])
