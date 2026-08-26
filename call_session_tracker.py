"""
In-Memory-Puffer für telephony_calls-Events, gruppiert nach callSessionId.

##########################################################################
# WARUM DAS NÖTIG WURDE (nach echten Testanrufen, 21.08.2026 / 26.08.2026) #
##########################################################################

telephony_calls feuert org-weit für JEDEN Anruf (nicht nur Warteschlangen-
Anrufe), pro Call-Leg (Telefon, Webex-App, ...) einzeln. Eine grobe "irgendein
Leg disconnected ohne answered"-Logik reicht nicht:
  - Ohne event="all" fehlen "answered"/"connected"-Events komplett.
  - Ein eigener, nicht angenommener Rückruf sah bisher GENAUSO aus wie ein in
    der Warteschlange verlorener Anruf (echter Testanruf am 26.08.2026 hat das
    bestätigt -> fälschlich als "verpasst" gepostet).

Strategie:
  1. Pro callSessionId alle Events sammeln (mehrere Legs möglich, ein Leg pro
     klingelndem Endpoint).
  2. Sobald IRGENDEIN Event dieser Session eventType "answered" zeigt oder
     state=="connected" -> Session DAUERHAFT als "beantwortet" markieren,
     nie mehr als verpasst werten (auch wenn spätere Legs noch disconnecten).
  3. Sobald IRGENDEIN Event dieser Session personality=="originator" zeigt
     (wir haben den Call SELBST gestartet, z. B. ein Rückruf) -> Session
     SOFORT und dauerhaft ausschließen, kein Grace-Timer, keine Karte. Nur
     personality=="terminator" (wir werden angerufen) kann ein verpasster
     Anruf sein.
  4. Nach dem jeweils letzten Event einer (noch nicht beantworteten,
     eingehenden) Session ein Grace-Fenster von GRACE_SECONDS abwarten. Kommt
     in der Zeit KEIN weiteres Event, UND alle bekannten Legs stehen auf
     "disconnected", UND die Session wurde nie beantwortet -> als
     "verpasster Anruf" werten und den übergebenen Callback aufrufen.

Implementierungsdetail: bei jedem Event wird ein neuer Grace-Check-Task
gestartet; ältere, noch schlafende Tasks erkennen beim Aufwachen über den
"generation"-Zähler, dass sie überholt sind, und tun dann nichts. Das ist
bewusst einfach gehalten (etwas Task-Overhead) statt ein Timer-Objekt manuell
zu canceln -- für dieses Test-Gerüst ausreichend, s. auch store.py zur
selben "bewusst simpel"-Haltung.

##########################################################################
# GELÖSTE LÜCKE: inbound vs. outbound (Stand 26.08.2026)                  #
##########################################################################

Vergleich zweier echter Testanrufe am 26.08.2026 (komplette Envelopes im
main.py-Log) zeigte den Unterschied klar:

  - Echter Warteschlangen-Anruf (verpasst): jedes Leg hatte
    personality="terminator", eventType beim ersten Event="received", UND
    ein `redirections`-Feld:
        "redirections": [{"reason": "callQueue",
                           "redirectingParty": {"name": "Zentrale",
                                                 "number": "+4989...",
                                                 "idType": "CALL_QUEUE", ...}}]

  - Eigener, nicht angenommener Rückruf: personality="originator",
    eventType beim ersten Event="originated", KEIN `redirections`-Feld.

personality ist also der zuverlässige Richtungs-Indikator (redundant mit
eventType "received" vs. "originated") und wird hier zum Filtern genutzt.
Das `redirections`-Feld liefert als Bonus den ECHTEN Warteschlangennamen
und die DNIS statt eines festen Platzhalters -- wird opportunistisch
übernommen, wenn vorhanden, sonst bleibt "Zentrale" als Fallback.

Basis sind bisher zwei reale Testanrufe -- sollte sich bei weiteren Tests
(z. B. Direktanruf einer internen Nebenstelle ohne Warteschlange) ein
Gegenbeispiel zeigen, muss diese Regel nachgeschärft werden.

##########################################################################
# FILTER AUF BESTIMMTE WARTESCHLANGEN (Stand 26.08.2026)                  #
##########################################################################

Per `target_queue_numbers` (aus WEBEX_TARGET_QUEUE_NUMBERS in .env, s.
config.py) kann optional eine Liste von Warteschlangen-Nummern (DNIS)
angegeben werden. Ist sie leer -> kein Filter, jeder nie beantwortete
eingehende Anruf zählt (aktuelles Default-Verhalten). Ist sie gesetzt ->
eine Session wird nur dann final als "verpasst" gewertet, wenn ihre
`queue_number` (aus data.redirections, s. oben) in der Liste steht. Anrufe
über andere Warteschlangen oder ganz ohne Warteschlange (kein redirections-
Feld) werden dann stillschweigend ignoriert (kein Karten-Post, aber auch
kein Fehler -- geloggt wird es trotzdem).
"""
import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("webex-teams-middleware.call_session_tracker")

GRACE_SECONDS = 8.0

# BUGFIX (Bug-Hunt 26.08.2026): self._sessions wuchs bisher unbegrenzt -- jede
# jemals gesehene callSessionId blieb für immer im Speicher (auch ausgehende/
# beantwortete/gefilterte Sessions). Für einen dauerhaft laufenden Prozess ist
# das ein Memory Leak. Sessions, die älter als RETENTION_SECONDS sind, werden
# deshalb bei jedem neuen Event mit-aufgeräumt (s. _prune_old_sessions_locked).
# 1h ist reichlich Puffer über das 8s-Grace-Fenster hinaus.
RETENTION_SECONDS = 3600.0

MissedCallCallback = Callable[[dict], Awaitable[None]]


@dataclass
class _CallLeg:
    call_id: str
    state: Optional[str] = None
    personality: Optional[str] = None
    endpoint_type: Optional[str] = None
    remote_party: dict = field(default_factory=dict)
    created_at: Optional[str] = None
    disconnected_at: Optional[str] = None


@dataclass
class _CallSession:
    session_id: str
    legs: dict = field(default_factory=dict)  # call_id -> _CallLeg
    answered: bool = False
    outbound: bool = False  # personality=="originator" gesehen -> wir haben selbst angerufen
    finalized: bool = False
    generation: int = 0
    last_event_timestamp: Optional[str] = None
    queue_name: Optional[str] = None
    queue_number: Optional[str] = None
    created_wall_time: float = field(default_factory=time.time)


class CallSessionTracker:
    def __init__(
        self,
        on_missed_call: MissedCallCallback,
        grace_seconds: float = GRACE_SECONDS,
        target_queue_numbers: Optional[set] = None,
    ) -> None:
        self._sessions: dict[str, _CallSession] = {}
        self._lock = threading.Lock()
        self._on_missed_call = on_missed_call
        self._grace_seconds = grace_seconds
        # Leer/None -> kein Filter (jede Warteschlange bzw. auch Nicht-Warteschlangen-
        # Anrufe zählen). Gesetzt -> nur Anrufe über eine dieser Nummern zählen, s.
        # Moduldocstring "FILTER AUF BESTIMMTE WARTESCHLANGEN".
        self._target_queue_numbers = target_queue_numbers or None

    def record_event(self, data: dict) -> None:
        """
        Nimmt den rohen `data`-Block EINES telephony_calls-Webhook-Events entgegen
        (unabhängig vom äußeren "event"-Feld -- created/updated/deleted werden alle
        gleich behandelt, entscheidend ist nur `data.eventType`/`data.state`).
        """
        session_id = data.get("callSessionId")
        call_id = data.get("callId")
        if not session_id or not call_id:
            logger.warning("telephony_calls-Event ohne callSessionId/callId, ignoriert: %s", data)
            return

        schedule_check = False
        my_generation = 0

        with self._lock:
            self._prune_old_sessions_locked()

            session = self._sessions.setdefault(session_id, _CallSession(session_id=session_id))
            if session.finalized:
                return  # Session bereits final gewertet -> weitere Events sind für uns uninteressant

            leg = session.legs.setdefault(call_id, _CallLeg(call_id=call_id))
            personality = data.get("personality")
            leg.personality = personality or leg.personality
            leg.endpoint_type = data.get("endpointType") or leg.endpoint_type
            leg.remote_party = data.get("remoteParty") or leg.remote_party
            leg.created_at = data.get("created") or leg.created_at

            state = data.get("state")
            if state:
                leg.state = state
            if data.get("disconnected"):
                leg.disconnected_at = data.get("disconnected")
                leg.state = "disconnected"

            session.last_event_timestamp = (
                data.get("disconnected") or data.get("eventTimestamp") or session.last_event_timestamp
            )

            if session.queue_name is None:
                for redirection in data.get("redirections") or []:
                    if redirection.get("reason") == "callQueue":
                        redirecting_party = redirection.get("redirectingParty") or {}
                        session.queue_name = redirecting_party.get("name")
                        # .strip(): BUGFIX -- Vergleiche gegen WEBEX_TARGET_QUEUE_NUMBERS/
                        # QUEUE_MAIL_ROUTES sind exakte String-Vergleiche; ein Whitespace-
                        # Unterschied hätte sonst eine Warteschlange silent nicht matchen lassen.
                        queue_number = redirecting_party.get("number")
                        session.queue_number = queue_number.strip() if queue_number else queue_number
                        break

            if personality == "originator" and not session.outbound:
                session.outbound = True
                session.finalized = True  # ausgehender Call -> für uns erledigt, kein Grace-Timer nötig
                logger.info(
                    "Call-Session %s als AUSGEHEND markiert (personality=originator) "
                    "-> wird nie als verpasster Anruf gewertet.",
                    session_id,
                )
                return  # kein schedule_check mehr -> Funktion hier sauber verlassen

            if data.get("eventType") == "answered" or state == "connected":
                if not session.answered:
                    logger.info(
                        "Call-Session %s als beantwortet markiert (eventType=%s state=%s) "
                        "-> wird nie als verpasst gewertet.",
                        session_id, data.get("eventType"), state,
                    )
                session.answered = True

            session.generation += 1
            my_generation = session.generation
            if not session.answered:
                schedule_check = True

        if schedule_check:
            asyncio.create_task(self._grace_check(session_id, my_generation))

    async def _grace_check(self, session_id: str, generation: int) -> None:
        await asyncio.sleep(self._grace_seconds)

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.finalized or session.answered or session.outbound:
                return
            if session.generation != generation:
                return  # inzwischen kam ein neueres Event -> dieser Timer ist überholt
            if not session.legs or not all(leg.state == "disconnected" for leg in session.legs.values()):
                return  # mindestens ein Leg noch ohne bekannten disconnected-Status -> abwarten

            if session.queue_number is None:
                # Kein "callQueue"-Redirect in irgendeinem Event dieser Session gesehen
                # -> Direktanruf auf eine einzelne Durchwahl/Nebenstelle, KEIN Warteschlangen-
                # oder Sammelanschluss-Anruf. Bewusst kein Alarm (echter Testanruf 26.08.2026
                # hat gezeigt: ohne diesen Check wurde das fälschlich mit dem "Zentrale"-
                # Platzhalter als verpasster Anruf gemeldet).
                logger.info(
                    "Call-Session %s: verpasst, aber kein callQueue-Redirect gefunden "
                    "-> Direktanruf auf Durchwahl, kein Alarm.",
                    session_id,
                )
                return

            if self._target_queue_numbers and session.queue_number not in self._target_queue_numbers:
                logger.info(
                    "Call-Session %s: verpasst, aber Warteschlange %s nicht in WEBEX_TARGET_QUEUE_NUMBERS "
                    "-> ignoriert.",
                    session_id, session.queue_number,
                )
                return

            session.finalized = True
            record = self._build_record(session)

        logger.info(
            "Call-Session %s: %.0fs ohne neues Event, nie beantwortet, alle Legs disconnected "
            "-> als verpasster Anruf gewertet.",
            session_id, self._grace_seconds,
        )
        await self._on_missed_call(record)

    def _build_record(self, session: _CallSession) -> dict:
        remote_party: dict = {}
        for leg in session.legs.values():
            if leg.remote_party:
                remote_party = leg.remote_party
                break

        return {
            "call_id": session.session_id,
            "caller_number": remote_party.get("number"),
            "caller_name": remote_party.get("name"),
            "timestamp_iso": session.last_event_timestamp,
            # Echte Warteschlange aus data.redirections, falls vorhanden (s. Moduldocstring).
            # BUGFIX: Platzhalter war fest "Zentrale" (nur für diesen einen Kunden korrekt) --
            # generischer Fallback, da queue_number an dieser Stelle ohnehin nie None ist
            # (s. Check weiter oben, bricht sonst schon vorher ab).
            "queue_name": session.queue_name or "Unbekannte Warteschlange",
            "queue_number": session.queue_number,
        }

    def _prune_old_sessions_locked(self) -> None:
        """Muss unter self._lock aufgerufen werden. S. RETENTION_SECONDS oben."""
        cutoff = time.time() - RETENTION_SECONDS
        stale_ids = [
            sid for sid, session in self._sessions.items()
            if session.created_wall_time < cutoff
        ]
        for sid in stale_ids:
            del self._sessions[sid]
