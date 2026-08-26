"""
Sehr simpler In-Memory-Store für offene "verpasste Anrufe".

ACHTUNG (Produktivhinweis): Das hier ist bewusst simpel gehalten, um den Ablauf
verständlich zu zeigen. Für den Produktivbetrieb NICHT so lassen, weil:

  - Bei einem Prozess-Neustart sind alle offenen Karten "vergessen" (Update auf eine
    alte Karte würde dann fehlschlagen, weil activity_id/conversation_id fehlen).
  - Bei mehreren Worker-Prozessen/Instanzen (z. B. hinter einem Load Balancer) sieht
    nicht jeder Prozess denselben Speicher -> Race Conditions trotz Lock.

Für Produktion: Redis (mit SETNX/WATCH für den atomaren "claim") oder eine
klassische DB mit einer UNIQUE-Constraint + "UPDATE ... WHERE claimed_by IS NULL"
als atomarem Claim-Mechanismus. Das Interface unten (claim_call) ist so geschnitten,
dass ein Austausch gegen Redis/Postgres ohne Änderungen am Rest des Codes möglich ist.

Unbegrenztes Speicherwachstum (jeder jemals gepostete Call blieb für immer im
Dict) ist per RETENTION_SECONDS behoben -- das löst NICHT die beiden Punkte
oben, nur den separaten "Prozess läuft wochenlang -> Speicher wächst endlos"-Bug.
"""
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# BUGFIX (Bug-Hunt 26.08.2026): self._records wuchs bisher unbegrenzt -- jeder
# jemals gepostete Call blieb für immer im Speicher, auch längst geklärte. Für
# einen dauerhaft laufenden Prozess ist das ein Memory Leak. 24h Aufbewahrung
# ist reichlich, damit ein Agent auch eine ältere, noch offene Karte anklicken
# kann, bevor der Datensatz verschwindet.
RETENTION_SECONDS = 24 * 3600.0


@dataclass
class MissedCallRecord:
    call_id: str
    caller_number: str
    caller_name: Optional[str]
    queue_name: str
    timestamp_iso: str
    # DNIS der Warteschlange (z. B. "+4989248815150") -- die tatsächlich angerufene
    # Nummer, nicht die des Anrufers. None, falls keine Warteschlange zuordenbar war.
    queue_number: Optional[str] = None

    # Wird gesetzt, sobald die Karte in einer Webex-Space gepostet wurde.
    webex_room_id: Optional[str] = None
    webex_message_id: Optional[str] = None

    # Claim-Status
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None

    created_wall_time: float = field(default_factory=time.time)


class MissedCallStore:
    def __init__(self) -> None:
        self._records: dict[str, MissedCallRecord] = {}
        self._lock = threading.Lock()

    def add(self, record: MissedCallRecord) -> None:
        with self._lock:
            self._prune_old_locked()
            self._records[record.call_id] = record

    def _prune_old_locked(self) -> None:
        """Muss unter self._lock aufgerufen werden. S. RETENTION_SECONDS oben."""
        cutoff = time.time() - RETENTION_SECONDS
        stale_ids = [
            cid for cid, record in self._records.items()
            if record.created_wall_time < cutoff
        ]
        for cid in stale_ids:
            del self._records[cid]

    def get(self, call_id: str) -> Optional[MissedCallRecord]:
        with self._lock:
            return self._records.get(call_id)

    def set_webex_reference(self, call_id: str, room_id: str, message_id: str) -> None:
        with self._lock:
            record = self._records.get(call_id)
            if record:
                record.webex_room_id = room_id
                record.webex_message_id = message_id

    def claim_call(self, call_id: str, agent_name: str) -> tuple[bool, Optional[MissedCallRecord]]:
        """
        Atomarer "Wer zuerst kommt, mahlt zuerst"-Claim.

        Returns:
            (True, record)  -> dieser Agent hat soeben erfolgreich übernommen
            (False, record) -> war schon von jemand anderem übernommen
            (False, None)   -> call_id unbekannt (z. B. Karte zu alt / Prozess neu gestartet)
        """
        with self._lock:
            record = self._records.get(call_id)
            if record is None:
                return False, None
            if record.claimed_by is not None:
                return False, record
            record.claimed_by = agent_name
            record.claimed_at = datetime.utcnow().isoformat() + "Z"
            return True, record


store = MissedCallStore()
