"""Durable scheduler core backed by SQLite + a content-addressed blob store.

Guarantees enforced here:

* A frame task is identified by (scene_id, version, frame, settings_hash)
  via ``identity.task_key``; re-submitting an edited scene creates new
  identities, so old blobs can never satisfy the new shot.
* Work is handed out through expiring leases.  Every lease creates a fresh
  attempt row with a unique attempt id.  A result is accepted only when
  that exact attempt is still the active lease of its worker.
* An image becomes a formal frame only after full-bytes validation
  (PNG signature, per-chunk CRC, complete zlib/IDAT decode, exact expected
  dimensions).  Half-uploaded or corrupt bytes are refused and the task
  returns to the queue for a new attempt.
* Scheduler restart reaps every stale lease; an old attempt reporting late
  finds its row superseded and cannot overwrite the newer result.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

from . import identity
from .png import PNGError, decode_png

MAX_ATTEMPTS = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenes (
    scene_id TEXT NOT NULL,
    version TEXT NOT NULL,
    doc TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (scene_id, version)
);
CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    scene_id TEXT NOT NULL,
    version TEXT NOT NULL,
    settings_hash TEXT NOT NULL,
    frame_start INTEGER NOT NULL,
    frame_end INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_key TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    version TEXT NOT NULL,
    frame INTEGER NOT NULL,
    settings_hash TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 10,
    last_error TEXT,
    lease_owner TEXT,
    lease_expires REAL,
    blob_sha TEXT,
    width INTEGER,
    height INTEGER,
    completed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_sub ON tasks(submission_id);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    task_key TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    lease_expires REAL NOT NULL,
    state TEXT NOT NULL,
    reported_at REAL,
    error TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_key);
CREATE TABLE IF NOT EXISTS blobs (
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    name TEXT,
    started_at REAL,
    last_seen REAL,
    current_attempt TEXT,
    current_task TEXT,
    completed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
"""


class StaleLease(Exception):
    """The attempt is no longer the active lease for its task."""


class InvalidImage(Exception):
    """The uploaded bytes failed full validation."""


class FarmStore:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.blob_dir = os.path.join(data_dir, "blobs")
        os.makedirs(self.blob_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "farm.db")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ utils
    def _event(self, kind: str, detail: Any) -> None:
        self._conn.execute(
            "INSERT INTO events(ts, kind, detail) VALUES (?,?,?)",
            (time.time(), kind, json.dumps(detail, ensure_ascii=False)),
        )

    def _blob_path(self, sha: str) -> str:
        return os.path.join(self.blob_dir, sha[:2], sha[2:] + ".png")

    def _task_view(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["settings"] = json.loads(d.pop("settings_json"))
        return d

    # -------------------------------------------------------------- submission
    def create_submission(
        self,
        submission_id: str,
        scene_doc: dict,
        settings: dict,
        frame_start: int,
        frame_end: int,
        now: Optional[float] = None,
    ) -> dict:
        now = now if now is not None else time.time()
        scene_id = str(scene_doc["scene_id"])
        version = identity.scene_version(scene_doc)
        s_hash = identity.settings_hash(settings)
        if frame_end < frame_start:
            raise ValueError("frame_end must be >= frame_start")
        total = int(scene_doc["total_frames"])
        if not (0 <= frame_start <= frame_end < total):
            raise ValueError(f"frame range must lie within 0..{total - 1}")

        created, reused = [], []
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO scenes(scene_id, version, doc, created_at)"
                " VALUES (?,?,?,?)",
                (scene_id, version, json.dumps(scene_doc, ensure_ascii=False), now),
            )
            self._conn.execute(
                "INSERT INTO submissions(submission_id, scene_id, version,"
                " settings_hash, frame_start, frame_end, status, created_at)"
                " VALUES (?,?,?,?,?,?, 'open', ?)",
                (submission_id, scene_id, version, s_hash,
                 frame_start, frame_end, now),
            )
            for frame in range(frame_start, frame_end + 1):
                tkey = identity.task_key(scene_id, version, frame, s_hash)
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO tasks(task_key, submission_id, scene_id,"
                    " version, frame, settings_hash, settings_json, status,"
                    " max_attempts, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?, 'pending', ?, ?, ?)",
                    (tkey, submission_id, scene_id, version, frame, s_hash,
                     json.dumps(settings, sort_keys=True), MAX_ATTEMPTS, now, now),
                )
                (created if cur.rowcount else reused).append(frame)
            self._event("submission", {
                "submission_id": submission_id, "scene_id": scene_id,
                "version": version, "settings_hash": s_hash,
                "frames": list(range(frame_start, frame_end + 1)),
                "reused": reused,
            })
            self._conn.commit()
        return {"submission_id": submission_id, "scene_id": scene_id,
                "version": version, "settings_hash": s_hash,
                "created_frames": created, "reused_frames": reused}

    def get_scene_doc(self, scene_id: str, version: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT doc FROM scenes WHERE scene_id=? AND version=?",
            (scene_id, version),
        ).fetchone()
        return json.loads(row["doc"]) if row else None

    def list_submissions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM submissions ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_submission(self, submission_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM submissions WHERE submission_id=?", (submission_id,)
        ).fetchone()
        return dict(row) if row else None

    def cancel_submission(self, submission_id: str, now: Optional[float] = None) -> dict:
        now = now if now is not None else time.time()
        with self._lock:
            sub = self.get_submission(submission_id)
            if not sub:
                raise KeyError(submission_id)
            if sub["status"] != "open":
                return sub
            self._conn.execute(
                "UPDATE submissions SET status='cancelled' WHERE submission_id=?",
                (submission_id,),
            )
            cur = self._conn.execute(
                "UPDATE tasks SET status='cancelled', lease_owner=NULL,"
                " lease_expires=NULL, updated_at=?"
                " WHERE submission_id=? AND status IN ('pending','leased')",
                (now, submission_id),
            )
            self._conn.execute(
                "UPDATE attempts SET state='superseded', reported_at=?"
                " WHERE state='active'"
                " AND task_key IN (SELECT task_key FROM tasks WHERE submission_id=?)",
                (now, submission_id),
            )
            self._event("cancel", {"submission_id": submission_id,
                                   "tasks_affected": cur.rowcount})
            self._conn.commit()
        return self.get_submission(submission_id)

    # ------------------------------------------------------------------- tasks
    def list_tasks(self, submission_id: Optional[str] = None) -> list[dict]:
        if submission_id:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE submission_id=? ORDER BY frame",
                (submission_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, frame"
            ).fetchall()
        return [self._task_view(r) for r in rows]

    def get_task(self, task_key: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_key=?", (task_key,)
        ).fetchone()
        return self._task_view(row) if row else None

    def task_provenance(self, task_key: str) -> dict:
        """Which worker/attempt produced the formal frame (audit trail)."""
        attempts = self._conn.execute(
            "SELECT attempt_id, worker_id, state, error, created_at, reported_at"
            " FROM attempts WHERE task_key=? ORDER BY created_at", (task_key,)
        ).fetchall()
        succeeded = next(
            (a for a in reversed(attempts) if a["state"] == "succeeded"), None
        )
        return {
            "attempts": [dict(a) for a in attempts],
            "completed_by": succeeded["worker_id"] if succeeded else None,
            "completed_attempt": succeeded["attempt_id"] if succeeded else None,
        }

    def summary(self, submission_id: str) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE submission_id=?"
            " GROUP BY status", (submission_id,),
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ----------------------------------------------------------------- workers
    def register_worker(self, worker_id: str, name: str, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO workers(worker_id, name, started_at, last_seen)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(worker_id) DO UPDATE SET name=excluded.name,"
                " last_seen=excluded.last_seen",
                (worker_id, name, now, now),
            )
            self._conn.commit()

    def touch_worker(self, worker_id: str, attempt_id: Optional[str],
                     task_key: Optional[str], now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE workers SET last_seen=?, current_attempt=?, current_task=?"
                " WHERE worker_id=?", (now, attempt_id, task_key, worker_id),
            )
            self._conn.commit()

    def list_workers(self, stale_after: float = 30.0) -> list[dict]:
        cutoff = time.time() - stale_after
        rows = self._conn.execute(
            "SELECT * FROM workers ORDER BY started_at"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["online"] = d["last_seen"] is not None and d["last_seen"] >= cutoff
            out.append(d)
        return out

    # ------------------------------------------------------------------- lease
    def claim_task(self, worker_id: str, lease_seconds: float,
                   now: Optional[float] = None) -> Optional[dict]:
        """Atomically claim one pending/expired task under a fresh lease."""
        now = now if now is not None else time.time()
        attempt_id = identity.sha256_hex(
            f"{worker_id}|{now}|{os.urandom(8).hex()}"
        )[:32]
        with self._lock:
            row = self._conn.execute(
                "SELECT t.* FROM tasks t JOIN submissions s"
                " ON t.submission_id=s.submission_id"
                " WHERE s.status='open' AND ("
                "  t.status='pending' OR"
                "  (t.status='leased' AND t.lease_expires < ?))"
                " ORDER BY t.created_at, t.frame LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            tkey = row["task_key"]
            if row["status"] == "leased":
                self._conn.execute(
                    "UPDATE attempts SET state='superseded', reported_at=?"
                    " WHERE task_key=? AND state='active'",
                    (now, tkey),
                )
                self._event("lease_expired", {"task_key": tkey,
                                              "old_owner": row["lease_owner"]})
            expires = now + lease_seconds
            self._conn.execute(
                "UPDATE tasks SET status='leased', lease_owner=?,"
                " lease_expires=?, attempt_count=attempt_count+1,"
                " updated_at=? WHERE task_key=?",
                (worker_id, expires, now, tkey),
            )
            self._conn.execute(
                "INSERT INTO attempts(attempt_id, task_key, worker_id,"
                " lease_expires, state, created_at) VALUES (?,?,?,?, 'active', ?)",
                (attempt_id, tkey, worker_id, expires, now),
            )
            self._conn.execute(
                "UPDATE workers SET last_seen=?, current_attempt=?, current_task=?"
                " WHERE worker_id=?", (now, attempt_id, tkey, worker_id),
            )
            self._event("claim", {"task_key": tkey, "worker_id": worker_id,
                                  "attempt_id": attempt_id})
            self._conn.commit()
            view = self._task_view(
                self._conn.execute(
                    "SELECT * FROM tasks WHERE task_key=?", (tkey,)
                ).fetchone()
            )
            view["attempt_id"] = attempt_id
            view["lease_expires"] = expires
            return view

    def heartbeat(self, attempt_id: str, worker_id: str, extend_seconds: float,
                  now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        with self._lock:
            att = self._conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if att is None or att["state"] != "active" or att["worker_id"] != worker_id:
                raise StaleLease("attempt is no longer active")
            expires = now + extend_seconds
            self._conn.execute(
                "UPDATE attempts SET lease_expires=? WHERE attempt_id=?",
                (expires, attempt_id),
            )
            self._conn.execute(
                "UPDATE tasks SET lease_expires=? WHERE task_key=?",
                (expires, att["task_key"]),
            )
            self._conn.execute(
                "UPDATE workers SET last_seen=? WHERE worker_id=?", (now, worker_id)
            )
            self._conn.commit()
            return expires

    def reap_expired(self, now: Optional[float] = None) -> int:
        """Move every expired lease back to the queue. Runs at startup too."""
        now = now if now is not None else time.time()
        reaped = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_key, attempt_count, max_attempts FROM tasks"
                " WHERE status='leased' AND lease_expires < ?", (now,)
            ).fetchall()
            for r in rows:
                tkey = r["task_key"]
                self._conn.execute(
                    "UPDATE attempts SET state='superseded', reported_at=?"
                    " WHERE task_key=? AND state='active'", (now, tkey),
                )
                if r["attempt_count"] >= r["max_attempts"]:
                    new_status, err = "failed", "max attempts reached"
                else:
                    new_status, err = "pending", None
                self._conn.execute(
                    "UPDATE tasks SET status=?, lease_owner=NULL,"
                    " lease_expires=NULL, last_error=COALESCE(?, last_error),"
                    " updated_at=? WHERE task_key=?",
                    (new_status, err, now, tkey),
                )
                self._event("reap", {"task_key": tkey, "new_status": new_status})
                reaped += 1
            self._conn.commit()
        return reaped

    # --------------------------------------------------------------- reporting
    def _validate_and_store(self, blob: bytes, expected_w: int,
                            expected_h: int, now: float) -> str:
        if not isinstance(blob, (bytes, bytearray)):
            raise InvalidImage("payload is not bytes")
        if len(blob) < 64:
            raise InvalidImage("payload suspiciously small")
        try:
            w, h, channels, raw = decode_png(bytes(blob))
        except PNGError as exc:
            raise InvalidImage(str(exc)) from exc
        if (w, h) != (expected_w, expected_h):
            raise InvalidImage(
                f"dimensions {w}x{h} != expected {expected_w}x{expected_h}"
            )
        sha = identity.sha256_hex(blob)
        path = self._blob_path(sha)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + f".tmp{os.getpid()}.{threading.get_ident()}"
            with open(tmp, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        self._conn.execute(
            "INSERT OR IGNORE INTO blobs(sha256, size, width, height, created_at)"
            " VALUES (?,?,?,?,?)",
            (sha, len(blob), w, h, now),
        )
        return sha

    def succeed_attempt(self, attempt_id: str, worker_id: str, blob: bytes,
                        now: Optional[float] = None) -> str:
        """Commit a rendered image. Returns 'ok', 'stale' or 'invalid'."""
        now = now if now is not None else time.time()
        with self._lock:
            att = self._conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if att is None or att["state"] != "active" or att["worker_id"] != worker_id:
                self._event("report_rejected", {"attempt_id": attempt_id,
                                                "worker_id": worker_id,
                                                "reason": "stale"})
                self._conn.commit()
                return "stale"
            task = self._conn.execute(
                "SELECT * FROM tasks WHERE task_key=?", (att["task_key"],)
            ).fetchone()
            if task is None or task["status"] != "leased" or task["lease_owner"] != worker_id:
                self._conn.execute(
                    "UPDATE attempts SET state='superseded', reported_at=?",
                    (now, attempt_id),
                )
                self._event("report_rejected", {"attempt_id": attempt_id,
                                                "reason": "task_not_leased"})
                self._conn.commit()
                return "stale"
            settings = json.loads(task["settings_json"])
            try:
                sha = self._validate_and_store(
                    blob, int(settings["width"]), int(settings["height"]), now
                )
            except InvalidImage as exc:
                self._conn.execute(
                    "UPDATE attempts SET state='failed', reported_at=?, error=?",
                    (now, str(exc)),
                )
                self._return_task_to_queue(task, now, f"invalid image: {exc}")
                self._conn.execute(
                    "UPDATE workers SET last_seen=?, failed=failed+1,"
                    " current_attempt=NULL, current_task=NULL WHERE worker_id=?",
                    (now, worker_id),
                )
                self._event("invalid_image", {"task_key": task["task_key"],
                                               "attempt_id": attempt_id,
                                               "error": str(exc)})
                self._conn.commit()
                return "invalid"
            self._conn.execute(
                "UPDATE tasks SET status='succeeded', blob_sha=?, width=?,"
                " height=?, completed_at=?, lease_owner=NULL, lease_expires=NULL,"
                " last_error=NULL, updated_at=? WHERE task_key=?",
                (sha, settings["width"], settings["height"], now, now,
                 task["task_key"]),
            )
            self._conn.execute(
                "UPDATE attempts SET state='succeeded', reported_at=? WHERE attempt_id=?",
                (now, attempt_id),
            )
            self._conn.execute(
                "UPDATE workers SET last_seen=?, completed=completed+1,"
                " current_attempt=NULL, current_task=NULL WHERE worker_id=?",
                (now, worker_id),
            )
            self._event("succeed", {"task_key": task["task_key"],
                                    "attempt_id": attempt_id, "blob": sha})
            self._conn.commit()
            return "ok"

    def fail_attempt(self, attempt_id: str, worker_id: str, error: str,
                     now: Optional[float] = None) -> str:
        now = now if now is not None else time.time()
        with self._lock:
            att = self._conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if att is None or att["state"] != "active" or att["worker_id"] != worker_id:
                self._conn.commit()
                return "stale"
            task = self._conn.execute(
                "SELECT * FROM tasks WHERE task_key=?", (att["task_key"],)
            ).fetchone()
            self._conn.execute(
                "UPDATE attempts SET state='failed', reported_at=?, error=?"
                " WHERE attempt_id=?", (now, error[:500], attempt_id),
            )
            if task is not None and task["status"] == "leased" and task["lease_owner"] == worker_id:
                self._return_task_to_queue(task, now, error[:500])
            self._conn.execute(
                "UPDATE workers SET last_seen=?, failed=failed+1,"
                " current_attempt=NULL, current_task=NULL WHERE worker_id=?",
                (now, worker_id),
            )
            self._event("fail", {"task_key": att["task_key"], "error": error[:200]})
            self._conn.commit()
            return "ok"

    def _return_task_to_queue(self, task: sqlite3.Row, now: float, error: str) -> None:
        if task["attempt_count"] >= task["max_attempts"]:
            self._conn.execute(
                "UPDATE tasks SET status='failed', lease_owner=NULL,"
                " lease_expires=NULL, last_error=?, updated_at=? WHERE task_key=?",
                (error, now, task["task_key"]),
            )
        else:
            self._conn.execute(
                "UPDATE tasks SET status='pending', lease_owner=NULL,"
                " lease_expires=NULL, last_error=?, updated_at=? WHERE task_key=?",
                (error, now, task["task_key"]),
            )

    # ------------------------------------------------------------------ output
    def frame_blob_path(self, task_key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT blob_sha FROM tasks WHERE task_key=? AND status='succeeded'",
            (task_key,),
        ).fetchone()
        if not row or not row["blob_sha"]:
            return None
        path = self._blob_path(row["blob_sha"])
        if not os.path.exists(path):
            return None
        return path

    def recent_events(self, limit: int = 30) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d["detail"])
            except (TypeError, json.JSONDecodeError):
                pass
            out.append(d)
        return out
