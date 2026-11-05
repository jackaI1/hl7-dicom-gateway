"""SQLite outbox and audit trail. Each state transition is one durable transaction."""

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Collision(ValueError):
    pass


class Store:
    def __init__(self, path: Path, *, max_attempts=3, backoff=2, lease=60):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_attempts, self.backoff, self.lease = max_attempts, backoff, lease
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload BLOB NOT NULL,
                    fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL,
                    error TEXT, created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ready ON jobs(status, due);
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY, time REAL NOT NULL,
                    job_id TEXT, event TEXT NOT NULL, detail TEXT NOT NULL
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, job_id, event, detail, now):
        db.execute(
            "INSERT INTO audit(time, job_id, event, detail) VALUES (?,?,?,?)",
            (now, job_id, event, detail),
        )

    def record(self, job_id, event, detail="", *, now=None):
        with self.connect() as db:
            self._event(db, job_id, event, detail, time.time() if now is None else now)

    def enqueue(self, job_id, kind, payload: bytes, *, fingerprint=None, now=None) -> bool:
        now = time.time() if now is None else now
        fingerprint = fingerprint or hashlib.sha256(payload).hexdigest()
        with self.connect() as db:
            old = db.execute("SELECT fingerprint FROM jobs WHERE id=?", (job_id,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise Collision("identity_collision")
                self._event(db, job_id, "duplicate", "", now)
                return False
            db.execute(
                "INSERT INTO jobs(id,kind,payload,fingerprint,due,created) VALUES(?,?,?,?,?,?)",
                (job_id, kind, payload, fingerprint, now, now),
            )
            self._event(db, job_id, "accepted", kind, now)
            return True

    def claim(self, *, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            expired = db.execute(
                "SELECT id,attempts FROM jobs WHERE status='processing' AND due<=?", (now,)
            ).fetchall()
            for row in expired:
                state = "dead" if row["attempts"] >= self.max_attempts else "pending"
                db.execute(
                    "UPDATE jobs SET status=?,error=? WHERE id=?",
                    (state, "lease_expired", row["id"]),
                )
                self._event(db, row["id"], state, "lease_expired", now)
            row = db.execute(
                "SELECT * FROM jobs WHERE status='pending' AND due<=? ORDER BY created,id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET status='processing', attempts=attempts+1, due=? WHERE id=?",
                (now + self.lease, row["id"]),
            )
            self._event(db, row["id"], "attempt", str(row["attempts"] + 1), now)
            result = dict(row)
            result["attempts"] += 1
            return result

    def complete(self, job_id, *, now=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='delivered',error=NULL WHERE id=?", (job_id,))
            self._event(db, job_id, "delivered", "", time.time() if now is None else now)

    def fail(self, job_id, code, *, permanent=False, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            row = db.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
            state = "dead" if permanent or row["attempts"] >= self.max_attempts else "pending"
            due = now + self.backoff * 2 ** (row["attempts"] - 1)
            db.execute(
                "UPDATE jobs SET status=?,due=?,error=? WHERE id=?", (state, due, code, job_id)
            )
            self._event(db, job_id, "dead" if state == "dead" else "retry", code, now)

    def replay(self, job_id, *, now=None) -> bool:
        now = time.time() if now is None else now
        with self.connect() as db:
            changed = db.execute(
                "UPDATE jobs SET status='pending',attempts=0,due=?,error=NULL "
                "WHERE id=? AND status='dead'",
                (now, job_id),
            ).rowcount
            if changed:
                self._event(db, job_id, "replayed", "", now)
            return bool(changed)

    def counts(self):
        with self.connect() as db:
            return dict(db.execute("SELECT status,count(*) FROM jobs GROUP BY status").fetchall())

    def dead(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,kind,attempts,error FROM jobs WHERE status='dead' ORDER BY created"
                )
            ]

    def audit(self, limit=100):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM audit ORDER BY sequence DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in reversed(rows)]
