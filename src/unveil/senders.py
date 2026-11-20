"""Local record of who sent which files, so repeat offenders can be watched or blocked.

Stored in SQLite at ~/.unveil/unveil.db (override with the UNVEIL_HOME env var).
Nothing leaves the machine.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import ScanResult

STATUSES = ("trusted", "unknown", "watch", "blocked")


def default_db_path() -> Path:
    home = Path(os.environ.get("UNVEIL_HOME", Path.home() / ".unveil"))
    home.mkdir(parents=True, exist_ok=True)
    return home / "unveil.db"


def norm(sender: str) -> str:
    return sender.strip().lower()


class SenderDB:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_db_path()
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS senders (
                sender TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'unknown',
                note TEXT NOT NULL DEFAULT '',
                updated TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT,
                filename TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                verdict TEXT NOT NULL,
                score INTEGER NOT NULL,
                techniques TEXT NOT NULL,
                scanned_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS scans_sender ON scans(sender);
            CREATE INDEX IF NOT EXISTS scans_sha ON scans(sha256);
        """)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def get(self, sender: str) -> dict | None:
        s = norm(sender)
        row = self.con.execute("SELECT * FROM senders WHERE sender=?", (s,)).fetchone()
        if not row:
            return None
        stats = self.con.execute(
            "SELECT COUNT(*) n, SUM(verdict='malicious') bad, SUM(verdict='suspicious') sus, MAX(scanned_at) last "
            "FROM scans WHERE sender=?", (s,)).fetchone()
        return {**dict(row), "scans": stats["n"], "malicious": stats["bad"] or 0,
                "suspicious": stats["sus"] or 0, "last_seen": stats["last"]}

    def set_status(self, sender: str, status: str, note: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        s = norm(sender)
        cur = self.get(s)
        self.con.execute(
            "INSERT INTO senders(sender,status,note,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(sender) DO UPDATE SET status=excluded.status, note=excluded.note, updated=excluded.updated",
            (s, status, note if note is not None else (cur or {}).get("note", ""), self._now()))
        self.con.commit()

    def record_scan(self, sender: str | None, filename: str, result: ScanResult) -> dict | None:
        """Log a scan. A sender who sends a malicious file is automatically put on 'watch'
        (never auto-blocked: blocking is the user's call). Returns the sender record."""
        s = norm(sender) if sender else None
        techniques = sorted({f.technique for f in result.findings if f.severity in ("high", "critical")})
        self.con.execute(
            "INSERT INTO scans(sender,filename,sha256,verdict,score,techniques,scanned_at) VALUES(?,?,?,?,?,?,?)",
            (s, filename, result.sha256, result.verdict, result.score, json.dumps(techniques), self._now()))
        self.con.commit()
        if not s:
            return None
        rec = self.get(s)
        if rec is None:
            self.set_status(s, "watch" if result.verdict == "malicious" else "unknown",
                            "auto: sent a file with hidden AI instructions" if result.verdict == "malicious" else "")
        elif result.verdict == "malicious" and rec["status"] in ("unknown", "trusted"):
            self.set_status(s, "watch", "auto: sent a file with hidden AI instructions")
        return self.get(s)

    def list(self) -> list[dict]:
        rows = self.con.execute("SELECT sender FROM senders ORDER BY updated DESC").fetchall()
        return [self.get(r["sender"]) for r in rows]

    def history(self, sender: str | None = None, limit: int = 50) -> list[dict]:
        if sender:
            rows = self.con.execute("SELECT * FROM scans WHERE sender=? ORDER BY id DESC LIMIT ?", (norm(sender), limit))
        else:
            rows = self.con.execute("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "techniques": json.loads(r["techniques"])} for r in rows]

    def seen_hash(self, sha256: str) -> list[dict]:
        rows = self.con.execute("SELECT * FROM scans WHERE sha256=? ORDER BY id DESC", (sha256,)).fetchall()
        return [dict(r) for r in rows]

    def remove(self, sender: str) -> None:
        self.con.execute("DELETE FROM senders WHERE sender=?", (norm(sender),))
        self.con.commit()
