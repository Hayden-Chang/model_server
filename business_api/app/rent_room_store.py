import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path


class RentRoomStore:
    """Independent ledger: no house content or TimeFragment principals live here."""

    def __init__(self, path: str):
        if path != ":memory:":
            file = Path(path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.touch(mode=0o600, exist_ok=True)
            file.chmod(0o600)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS rr_sessions (
                token_hash TEXT PRIMARY KEY, account TEXT NOT NULL,
                openid TEXT NOT NULL, session_key TEXT NOT NULL, expires INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rr_orders (
                id TEXT PRIMARY KEY, account TEXT NOT NULL, openid TEXT NOT NULL,
                request_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'unpaid',
                wx_id TEXT UNIQUE, created INTEGER NOT NULL, last_checked INTEGER NOT NULL DEFAULT 0,
                UNIQUE(account, request_id)
            );
            CREATE INDEX IF NOT EXISTS rr_order_account ON rr_orders(account);
        """)

    def close(self):
        self.db.close()

    def create_session(self, account, openid, encrypted_key):
        token = secrets.token_urlsafe(32)
        with self.lock, self.db:
            self.db.execute("DELETE FROM rr_sessions WHERE expires < ?", (int(time.time()),))
            self.db.execute("INSERT INTO rr_sessions VALUES (?,?,?,?,?)", (
                hashlib.sha256(token.encode()).hexdigest(), account, openid,
                encrypted_key, int(time.time()) + 7200))
        return token

    def session(self, token):
        with self.lock:
            return self.db.execute("SELECT * FROM rr_sessions WHERE token_hash=? AND expires>?", (
                hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()

    def packs(self, account):
        with self.lock:
            return self.db.execute("SELECT count(*) FROM rr_orders WHERE account=? AND state='paid'", (account,)).fetchone()[0]

    def order(self, account, request_id):
        with self.lock:
            return self.db.execute("SELECT * FROM rr_orders WHERE account=? AND request_id=?", (account, request_id)).fetchone()

    def by_id(self, order_id):
        with self.lock:
            return self.db.execute("SELECT * FROM rr_orders WHERE id=?", (order_id,)).fetchone()

    def create_order(self, session, request_id):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.order(session["account"], request_id)
            if existing:
                return existing
            recent = self.db.execute("SELECT count(*) FROM rr_orders WHERE account=? AND created>?", (
                session["account"], int(time.time()) - 300)).fetchone()[0]
            if recent >= 10:
                raise ValueError("下单过于频繁，请稍后重试")
            self.db.execute("INSERT INTO rr_orders(id,account,openid,request_id,created) VALUES (?,?,?,?,?)", (
                secrets.token_hex(16), session["account"], session["openid"], request_id, int(time.time())))
            return self.order(session["account"], request_id)

    def orders(self, account):
        with self.lock:
            return self.db.execute("SELECT * FROM rr_orders WHERE account=? AND state NOT IN ('closed','refunded') AND last_checked<? ORDER BY created DESC", (account, int(time.time()) - 300)).fetchall()

    def checked(self, order_id):
        with self.lock, self.db:
            self.db.execute("UPDATE rr_orders SET last_checked=? WHERE id=?", (int(time.time()), order_id))

    def apply(self, order_id, state, wx_id=None):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.by_id(order_id)
            # Refunds are terminal. A delayed paid query cannot resurrect them.
            if not row or row["state"] == "refunded":
                return
            if state in ("unpaid", "pending", "closed") and row["state"] == "paid":
                return
            self.db.execute("UPDATE rr_orders SET state=?, wx_id=coalesce(?,wx_id), last_checked=? WHERE id=?", (state, wx_id, int(time.time()), order_id))
