"""Version 3 wallet; legacy capacity responses and local house content stay separate."""
import time
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


class MigrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    # The server can verify purchases, but housing occupancy exists only locally.
    active: int = Field(ge=0, le=1_000_000)


class CreditOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestId: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")
    action: Literal["reserve", "commit", "cancel"]


class CreditWallets:
    def __init__(self, ledger):
        self.ledger = ledger
        with ledger.lock, ledger.db:
            ledger.db.executescript("""
                CREATE TABLE IF NOT EXISTS rr_credit_wallets (
                    account TEXT PRIMARY KEY, free_grant INTEGER NOT NULL,
                    legacy_used INTEGER NOT NULL, migrated INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rr_credit_operations (
                    account TEXT NOT NULL, request_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('reserved','committed','cancelled')),
                    created INTEGER NOT NULL, PRIMARY KEY(account,request_id)
                );
            """)

    def _snapshot(self, account):
        db = self.ledger.db
        wallet = db.execute("SELECT * FROM rr_credit_wallets WHERE account=?", (account,)).fetchone()
        if not wallet:
            return {"version": 1, "migrated": False}
        # Each verified order is counted once, including old orders paid after
        # migration. Refunds remove their grant without deleting housing data.
        paid = db.execute("SELECT coalesce(sum(pack_size),0) FROM rr_orders WHERE account=? AND state='paid'", (account,)).fetchone()[0]
        spent = db.execute("SELECT count(*) FROM rr_credit_operations WHERE account=? AND state IN ('reserved','committed')", (account,)).fetchone()[0]
        return {"version": 1, "migrated": True,
                "remaining": max(0, wallet["free_grant"] + paid - wallet["legacy_used"] - spent)}

    def snapshot(self, account):
        with self.ledger.lock, self.ledger.db:
            self.ledger.db.execute("BEGIN")
            return self._snapshot(account)

    def migrate(self, account, active, free):
        with self.ledger.lock, self.ledger.db:
            self.ledger.db.execute("BEGIN IMMEDIATE")
            if not self.ledger.db.execute("SELECT 1 FROM rr_credit_wallets WHERE account=?", (account,)).fetchone():
                used = min(active, free + 10 * self.ledger.packs(account))
                self.ledger.db.execute("INSERT INTO rr_credit_wallets VALUES (?,?,?,?)", (account, free, used, int(time.time())))
            return self._snapshot(account)

    def operate(self, account, request_id, action):
        db = self.ledger.db
        with self.ledger.lock, db:
            db.execute("BEGIN IMMEDIATE")
            balance = self._snapshot(account)
            if not balance["migrated"]:
                raise ValueError("请先确认原有房源点数")
            old = db.execute("SELECT state FROM rr_credit_operations WHERE account=? AND request_id=?", (account, request_id)).fetchone()
            state = old["state"] if old else None
            if old is None:
                recent = db.execute("SELECT count(*) FROM rr_credit_operations WHERE account=? AND created>?", (account, int(time.time()) - 300)).fetchone()[0]
                if recent >= 100:
                    raise ValueError("操作过于频繁，请稍后重试")
            if action == "reserve" and state is None:
                if not balance["remaining"]:
                    raise ValueError("房源点数不足，请先购买房源点数")
                state = "reserved"
            elif action == "commit":
                if state not in ("reserved", "committed"):
                    raise ValueError("创建记录尚未确认，请重试")
                state = "committed"
            elif action == "cancel":
                # Completed creations are terminal even if the client restored
                # an older local snapshot. Report the outcome; never refund it.
                if state != "committed":
                    # Also blocks an in-flight reserve that arrives late.
                    state = "cancelled"
            if old:
                db.execute("UPDATE rr_credit_operations SET state=? WHERE account=? AND request_id=?", (state, account, request_id))
            else:
                db.execute("INSERT INTO rr_credit_operations VALUES (?,?,?,?)", (account, request_id, state, int(time.time())))
            return {"operation": {"requestId": request_id, "state": state}, "credits": self._snapshot(account)}


def install_credit_routes(app, ledger, current_session, settings):
    wallets = CreditWallets(ledger)

    def require_version(request, session):
        if request.headers.get("x-rent-room-capacity-version") != "3" or session["provider"] != "wechat":
            raise HTTPException(409, detail={"message": "请使用支持房源点数的小程序版本"})

    @app.post("/api/rent-room/credits/migrate")
    async def migrate(body: MigrationRequest, request: Request, session=Depends(current_session)):
        require_version(request, session)
        return {"credits": wallets.migrate(session["account"], body.active, settings.free_capacity)}

    @app.post("/api/rent-room/credits/operations")
    async def operate(body: CreditOperation, request: Request, session=Depends(current_session)):
        require_version(request, session)
        try:
            return wallets.operate(session["account"], body.requestId, body.action)
        except ValueError as error:
            code = "HOUSE_CAPACITY" if "不足" in str(error) else "CREDIT_OPERATION_CONFLICT"
            raise HTTPException(409, detail={"code": code, "message": str(error)}) from error

    return wallets
