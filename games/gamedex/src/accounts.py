"""Single-admin accounts + sessions, kept on the same PVC everything else uses.

Gamedex was public and unauthenticated by design (see prefs.py). This adds ONE
privileged account so the public keeps a read-only view while writes (fixing
mappings, uploading box art, refreshing) and a couple of sensitive reads (the NAS
file index, the RomM play links) become admin-only. No registration, no roles —
there is exactly one user, seeded on first boot from the environment.

Two tiny tables on their own SQLite file on /data, following prefs.py's shape (a
thread-locked connection, tables created in __init__). Passwords are bcrypt
(salt embedded in the hash); sessions are opaque 256-bit tokens carried in an
HttpOnly cookie, stored server-side so logout and password changes can revoke
them for real rather than just clearing the browser's copy.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import bcrypt

log = logging.getLogger("gamedex.accounts")

# A valid bcrypt hash of a random string. verify_password() runs checkpw against
# this when the username is unknown, so a missing user costs the same ~time as a
# wrong password and can't be told apart by a stopwatch.
_DUMMY_HASH = bcrypt.hashpw(secrets.token_bytes(16), bcrypt.gensalt(rounds=12))

_BCRYPT_ROUNDS = 12
_MAX_PW_BYTES = 72          # bcrypt silently truncates past this; be explicit


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class Accounts:
    def __init__(self, db_path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS users("
            " id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,"
            " pw_hash TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS sessions("
            " token TEXT PRIMARY KEY, user_id INTEGER NOT NULL,"
            " created_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS ix_sessions_exp ON sessions(expires_at)"
        )
        self._db.commit()

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _hash(password: str) -> str:
        pw = password.encode("utf-8")[:_MAX_PW_BYTES]
        return bcrypt.hashpw(pw, bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")

    # ---- bootstrap --------------------------------------------------------
    def user_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def bootstrap(self, username: str, password: str) -> bool:
        """Create the admin the first time only. A no-op once any user exists, so
        it's safe to call every boot and rotating the seed env never resets the
        password. Returns True iff it created the account."""
        if not username or not password:
            return False
        with self._lock:
            if self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                return False
            self._db.execute(
                "INSERT INTO users(username, pw_hash, created_at) VALUES(?,?,?)",
                (username, self._hash(password), _iso(_now())),
            )
            self._db.commit()
        return True

    # ---- auth -------------------------------------------------------------
    def verify_password(self, username: str, password: str) -> int | None:
        """Return the user id on a correct password, else None. Always runs a
        bcrypt check (against a dummy hash for an unknown user) so timing doesn't
        leak whether the username exists."""
        with self._lock:
            row = self._db.execute(
                "SELECT id, pw_hash FROM users WHERE username=?", (username,)
            ).fetchone()
        pw = (password or "").encode("utf-8")[:_MAX_PW_BYTES]
        if row is None:
            bcrypt.checkpw(pw, _DUMMY_HASH)          # burn the same time, then fail
            return None
        uid, pw_hash = row
        if bcrypt.checkpw(pw, pw_hash.encode("ascii")):
            return uid
        return None

    def set_password(self, user_id: int, new_password: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE users SET pw_hash=? WHERE id=?",
                (self._hash(new_password), user_id),
            )
            self._db.commit()

    # ---- sessions ---------------------------------------------------------
    def create_session(self, user_id: int, ttl_days: int = 30) -> str:
        token = secrets.token_urlsafe(32)           # 256 bits, opaque, server-side
        now = _now()
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions(token, user_id, created_at, expires_at)"
                " VALUES(?,?,?,?)",
                (token, user_id, _iso(now), _iso(now + timedelta(days=ttl_days))),
            )
            self._db.commit()
        return token

    def resolve_session(self, token: str | None) -> dict | None:
        """{'user_id','username'} for a live session, else None. An expired row is
        deleted on the way out so the table can't accrete dead sessions."""
        if not token:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT s.expires_at, s.user_id, u.username"
                " FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            expires_at, user_id, username = row
            try:
                expired = datetime.fromisoformat(expires_at) <= _now()
            except ValueError:
                expired = True
            if expired:
                self._db.execute("DELETE FROM sessions WHERE token=?", (token,))
                self._db.commit()
                return None
        return {"user_id": user_id, "username": username}

    def revoke_session(self, token: str) -> None:
        if not token:
            return
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE token=?", (token,))
            self._db.commit()

    def revoke_user_sessions(self, user_id: int, keep: str | None = None) -> None:
        """Drop every session for a user except optionally one (the caller's own,
        so changing your password doesn't log YOU out — just everyone else)."""
        with self._lock:
            if keep:
                self._db.execute(
                    "DELETE FROM sessions WHERE user_id=? AND token<>?", (user_id, keep)
                )
            else:
                self._db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._db.commit()

    def purge_expired(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE expires_at<=?", (_iso(_now()),))
            self._db.commit()
