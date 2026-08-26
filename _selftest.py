"""
Schneller lokaler Selbsttest der telephony_calls-Auswertung (Missed-Call-Erkennung
inkl. aller seit 21.08.2026 gefixten Falsch-Alarm-Fälle) + Claim-Race, mit
gemockten Webex-API-Calls (kein echtes Netzwerk nötig). Nicht Teil des
Produktivcodes, nur zur Verifikation während der Entwicklung.

Ausführen:
    .venv/Scripts/python.exe _selftest.py

WICHTIG zur Env-Reihenfolge: GRAPH_CLIENT_ID/GRAPH_TENANT_ID/MAIL_TO werden hier
explizit auf leer GESETZT (nicht nur setdefault!) BEVOR irgendein Modul importiert
wird, das config.py (und damit load_dotenv()) transitiv zieht. Sonst lädt
load_dotenv() die ECHTEN Werte aus der echten .env, und dieser "kein echtes
Netzwerk nötig"-Selbsttest würde bei jedem Lauf tatsächlich versuchen, eine echte
E-Mail über Microsoft Graph zu verschicken (Bug, gefunden beim Bug-Hunt 26.08.2026).
MISSED_CALL_GRACE_SECONDS wird kurz gesetzt, damit der Test nicht 8s pro Szenario
braucht.
"""
import os
import time

os.environ["WEBEX_WEBHOOK_SECRET"] = ""
os.environ["WEBEX_SPACE_ID"] = "Y2lzY29zcGFyazovL1JPT00vdGVzdA"
os.environ["GRAPH_CLIENT_ID"] = ""
os.environ["GRAPH_TENANT_ID"] = ""
os.environ["GRAPH_CLIENT_SECRET"] = ""
os.environ["MAIL_TO"] = ""
os.environ["QUEUE_MAIL_ROUTES"] = ""
os.environ["WEBEX_TARGET_QUEUE_NUMBERS"] = ""
os.environ["MISSED_CALL_GRACE_SECONDS"] = "1"

import webex_bot_client
from fastapi.testclient import TestClient

posted: dict = {}
deleted: list = []
dms: list = []


async def fake_post_card_to_space(card, room_id, fallback_text="x"):
    mid = f"msg-{len(posted) + 1}"
    posted[mid] = {"card": card, "room_id": room_id}
    return mid


async def fake_delete_message(message_id):
    deleted.append(message_id)


async def fake_get_attachment_action(action_id):
    return ACTION_FIXTURES[action_id]


async def fake_get_person_display_name(person_id):
    return {"person-A": "Agent A", "person-B": "Agent B"}.get(person_id, "Unbekannt")


async def fake_send_direct_message(person_id, markdown):
    dms.append((person_id, markdown))


webex_bot_client.post_card_to_space = fake_post_card_to_space
webex_bot_client.delete_message = fake_delete_message
webex_bot_client.get_attachment_action = fake_get_attachment_action
webex_bot_client.get_person_display_name = fake_get_person_display_name
webex_bot_client.send_direct_message = fake_send_direct_message

import main  # noqa: E402  (nach den Monkeypatches importieren)

GRACE_WAIT = 1.5  # > MISSED_CALL_GRACE_SECONDS oben, s. Kommentare unten

ACTION_FIXTURES = {
    "action-A": {"id": "action-A", "personId": "person-A", "roomId": "room-1", "messageId": "msg-1",
                 "inputs": {"call_id": None}},
    "action-B": {"id": "action-B", "personId": "person-B", "roomId": "room-1", "messageId": "msg-1",
                 "inputs": {"call_id": None}},
}


def _queue_call_data(session_id, call_id, *, with_redirect=True, personality="terminator",
                      answered=False, number="+4917662282090"):
    """Baut ein realistisches telephony_calls data-Objekt, s. call_session_tracker.py."""
    data = {
        "eventType": "answered" if answered else "disconnected",
        "eventTimestamp": "2026-08-26T13:00:00.000Z",
        "callId": call_id,
        "callSessionId": session_id,
        "personality": personality,
        "state": "connected" if answered else "disconnected",
        "remoteParty": {"number": number, "callType": "external"},
        "created": "2026-08-26T12:59:55.000Z",
    }
    if not answered:
        data["disconnected"] = "2026-08-26T13:00:00.000Z"
    if with_redirect:
        data["redirections"] = [{
            "reason": "callQueue",
            "redirectingParty": {"name": "Zentrale", "number": "+4989248815150", "idType": "CALL_QUEUE"},
        }]
    return data


def _post_telephony_event(client, data, event="deleted"):
    payload = {"id": "webhook-event", "resource": "telephony_calls", "event": event, "data": data}
    r = client.post("/webhooks/webex", json=payload)
    assert r.status_code == 200, r.text


def main_test():
    with TestClient(main.app) as client:
        # 1) Echter Warteschlangen-Anruf (nie beantwortet, callQueue-Redirect vorhanden) -> Karte
        session_1 = "test-session-queue-missed"
        _post_telephony_event(client, _queue_call_data(session_1, "leg-1"))
        time.sleep(GRACE_WAIT)
        assert len(posted) == 1, f"Verpasster Warteschlangen-Anruf haette eine Karte posten sollen: {posted}"
        print(f"OK: Verpasster Warteschlangen-Anruf -> Karte gepostet ({list(posted.keys())[0]})")

        # 2) Zwei "gleichzeitige" Klicks auf Uebernehmen -> nur einer gewinnt
        ACTION_FIXTURES["action-A"]["inputs"]["call_id"] = session_1
        ACTION_FIXTURES["action-B"]["inputs"]["call_id"] = session_1
        click_a = {"resource": "attachmentActions", "event": "created", "data": {"id": "action-A"}}
        click_b = {"resource": "attachmentActions", "event": "created", "data": {"id": "action-B"}}
        r1 = client.post("/webhooks/webex", json=click_a)
        r2 = client.post("/webhooks/webex", json=click_b)
        assert r1.status_code == 200 and r2.status_code == 200

        assert len(deleted) == 1, f"Alte Karte haette genau 1x geloescht werden sollen: {deleted}"
        assert len(posted) == 2, f"Es haette genau 1 gruene Bestaetigungskarte dazukommen sollen: {posted}"
        assert len(dms) == 1, f"Der zu spaete Klicker haette eine DM bekommen sollen: {dms}"
        assert "Agent A" in dms[0][1], dms
        print("OK: Race Condition korrekt aufgeloest (Agent A gewinnt, Agent B bekommt DM)")

        # 3) Warteschlangen-Anruf, der DOCH beantwortet wurde -> KEINE Karte
        before = len(posted)
        session_2 = "test-session-queue-answered"
        _post_telephony_event(client, _queue_call_data(session_2, "leg-2", answered=True), event="updated")
        _post_telephony_event(client, _queue_call_data(session_2, "leg-2"))  # danach normal aufgelegt
        time.sleep(GRACE_WAIT)
        assert len(posted) == before, "Beantworteter Anruf haette KEINE Karte posten duerfen"
        print("OK: Beantworteter Warteschlangen-Anruf korrekt ignoriert")

        # 4) Eigener ausgehender Rueckruf (personality=originator), nicht angenommen -> KEINE Karte
        before = len(posted)
        session_3 = "test-session-outbound"
        _post_telephony_event(
            client,
            _queue_call_data(session_3, "leg-3", personality="originator", with_redirect=False),
        )
        time.sleep(GRACE_WAIT)
        assert len(posted) == before, "Ausgehender Rueckruf haette KEINE Karte posten duerfen"
        print("OK: Ausgehender Rueckruf (personality=originator) korrekt ignoriert")

        # 5) Direktanruf auf eine Durchwahl (kein callQueue-Redirect) -> KEINE Karte
        before = len(posted)
        session_4 = "test-session-direct-extension"
        _post_telephony_event(client, _queue_call_data(session_4, "leg-4", with_redirect=False))
        time.sleep(GRACE_WAIT)
        assert len(posted) == before, "Direktanruf ohne Warteschlange haette KEINE Karte posten duerfen"
        print("OK: Direktanruf ohne callQueue-Redirect korrekt ignoriert")

    print("\nALLE TESTS BESTANDEN")


if __name__ == "__main__":
    main_test()
