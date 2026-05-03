"""
SQLite history backend — default, zero-config, runs on Raspberry Pi Zero.
Uses a single .db file at ~/.opensoil/history/<box_id>.db
"""

import sqlite3
import json
import time
import numpy as np
from pathlib import Path
from typing import Optional
from ..ihistory_store import (
    IHistoryStore, SensorReading, Event, LLMDecision, Session
)


class SQLiteHistoryStore(IHistoryStore):

    def __init__(self, box_id: str, db_path: Optional[Path] = None):
        self.box_id = box_id
        if db_path is None:
            db_path = Path.home() / ".opensoil" / "history" / f"{box_id}.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sensor_readings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL NOT NULL,
                box_id      TEXT NOT NULL,
                sensor_id   TEXT NOT NULL,
                value       REAL NOT NULL,
                unit        TEXT,
                phase       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_readings
                ON sensor_readings (box_id, sensor_id, ts);

            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL NOT NULL,
                box_id      TEXT NOT NULL,
                type        TEXT NOT NULL,
                severity    TEXT NOT NULL,
                sensor_id   TEXT,
                value       REAL,
                threshold   REAL,
                note        TEXT,
                photo_path  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events
                ON events (box_id, type, ts);

            CREATE TABLE IF NOT EXISTS llm_decisions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL NOT NULL,
                box_id          TEXT NOT NULL,
                phase           TEXT,
                full_prompt     TEXT,
                raw_response    TEXT,
                commands        TEXT,
                model           TEXT,
                latency_ms      INTEGER,
                tokens_used     INTEGER,
                was_overridden  INTEGER DEFAULT 0,
                reason          TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_decisions
                ON llm_decisions (box_id, ts);

            CREATE TABLE IF NOT EXISTS sessions (
                id          TEXT PRIMARY KEY,
                box_id      TEXT NOT NULL,
                domain_id   TEXT NOT NULL,
                profile_id  TEXT,
                started_at  REAL NOT NULL,
                ended_at    REAL,
                outcome     TEXT,
                notes       TEXT,
                rootcause   TEXT
            );
        """)
        self.conn.commit()

    # ── Writes ──────────────────────────────────────────────────────────────

    def write_reading(self, r: SensorReading) -> None:
        self.conn.execute(
            "INSERT INTO sensor_readings (ts,box_id,sensor_id,value,unit,phase) "
            "VALUES (?,?,?,?,?,?)",
            (r.ts, r.box_id, r.sensor_id, r.value, r.unit, r.phase)
        )
        self.conn.commit()

    def write_event(self, e: Event) -> None:
        self.conn.execute(
            "INSERT INTO events (ts,box_id,type,severity,sensor_id,value,threshold,note,photo_path) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (e.ts, e.box_id, e.type, e.severity,
             e.sensor_id, e.value, e.threshold, e.note, e.photo_path)
        )
        self.conn.commit()

    def write_decision(self, d: LLMDecision) -> None:
        self.conn.execute(
            "INSERT INTO llm_decisions "
            "(ts,box_id,phase,full_prompt,raw_response,commands,model,"
            " latency_ms,tokens_used,was_overridden,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (d.ts, d.box_id, d.phase, d.full_prompt, d.raw_response,
             json.dumps(d.commands), d.model, d.latency_ms,
             d.tokens_used, int(d.was_overridden), d.reason)
        )
        self.conn.commit()

    # ── Queries ─────────────────────────────────────────────────────────────

    def sensor_avg(self, box_id: str, window: int) -> dict:
        since = time.time() - window
        rows = self.conn.execute(
            "SELECT sensor_id, AVG(value) as avg FROM sensor_readings "
            "WHERE box_id=? AND ts>=? GROUP BY sensor_id",
            (box_id, since)
        ).fetchall()
        return {r["sensor_id"]: round(r["avg"], 2) for r in rows}

    def sensor_minmax(self, box_id: str, window: int) -> dict:
        since = time.time() - window
        rows = self.conn.execute(
            "SELECT sensor_id, MIN(value) as mn, MAX(value) as mx "
            "FROM sensor_readings WHERE box_id=? AND ts>=? GROUP BY sensor_id",
            (box_id, since)
        ).fetchall()
        return {
            r["sensor_id"]: {"min": round(r["mn"], 2), "max": round(r["mx"], 2)}
            for r in rows
        }

    def sensor_trend(self, box_id: str, window: int) -> dict:
        """Linear regression slope per day for each sensor."""
        since = time.time() - window
        rows = self.conn.execute(
            "SELECT sensor_id, ts, value FROM sensor_readings "
            "WHERE box_id=? AND ts>=? ORDER BY ts",
            (box_id, since)
        ).fetchall()
        from itertools import groupby
        result = {}
        data = [(r["sensor_id"], r["ts"], r["value"]) for r in rows]
        for sid, group in groupby(data, key=lambda x: x[0]):
            pts = list(group)
            if len(pts) < 3:
                continue
            xs = [(p[1] - pts[0][1]) / 86400 for p in pts]  # days
            ys = [p[2] for p in pts]
            slope = float(np.polyfit(xs, ys, 1)[0])
            result[sid] = round(slope, 3)
        return result

    def events(self, box_id: str, type: Optional[str] = None,
               since: Optional[float] = None) -> list:
        q = ("SELECT ts,box_id,type,severity,sensor_id,value,threshold,note,photo_path "
             "FROM events WHERE box_id=?")
        params = [box_id]
        if type:
            q += " AND type=?"; params.append(type)
        if since:
            q += " AND ts>=?"; params.append(since)
        q += " ORDER BY ts DESC"
        rows = self.conn.execute(q, params).fetchall()
        return [Event(**dict(r)) for r in rows]

    def llm_decisions(self, box_id: str, limit: int = 5) -> list:
        rows = self.conn.execute(
            "SELECT ts,box_id,phase,full_prompt,raw_response,commands,model,"
            "       latency_ms,tokens_used,was_overridden,reason "
            "FROM llm_decisions WHERE box_id=? ORDER BY ts DESC LIMIT ?",
            (box_id, limit)
        ).fetchall()
        decisions = []
        for r in rows:
            d = dict(r)
            d["commands"] = json.loads(d["commands"])
            d["was_overridden"] = bool(d["was_overridden"])
            decisions.append(LLMDecision(**d))
        return decisions

    def create_session(self, session: Session) -> str:
        self.conn.execute(
            "INSERT INTO sessions (id,box_id,domain_id,profile_id,started_at,outcome) "
            "VALUES (?,?,?,?,?,?)",
            (session.id, session.box_id, session.domain_id,
             session.profile_id, session.started_at, "active")
        )
        self.conn.commit()
        return session.id

    def close_session(self, session_id: str, outcome: str,
                      notes: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE sessions SET ended_at=?, outcome=?, notes=? WHERE id=?",
            (time.time(), outcome, notes, session_id)
        )
        self.conn.commit()
