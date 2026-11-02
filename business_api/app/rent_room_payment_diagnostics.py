"""Payment-only diagnostics. Never log payloads, credentials or WeChat identity."""
import json
import logging
import re
import time
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field


logger = logging.getLogger("rent_room.payment")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())


def safe_text(value, *, secrets=(), limit=240):
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[redacted]")
    text = re.sub(r"https?://\S+", "[url]", text, flags=re.I)
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(r'''(?ix)(["']?(?:access_token|token|session_key|sessionKey|openid|open_id|appsecret|appkey|pay_sig|paySig|signature|signData|authorization)["']?\s*[:=]\s*)["']?[^\s,;}&]+''', r"\1[redacted]", text)
    text = re.sub(r"[A-Za-z0-9_+/=-]{32,}", "[redacted]", text)
    return " ".join(text.split())[:limit]


def payment_log(stage, order=None, **fields):
    entry = {"event": "rent_room_payment", "stage": stage, "time": int(time.time())}
    if order is not None:
        entry.update(orderId=order["id"])
        if "request_id" in order.keys():
            entry["requestId"] = order["request_id"]
    entry.update(fields)
    logger.info(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))


class PaymentDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    eventId: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    stage: Literal["native_start", "native_success", "native_fail", "retry_confirmed"]
    errCode: int | None = Field(default=None, ge=-2147483648, le=2147483647)
    errMsg: str = Field(default="", max_length=1024)
    durationMs: int = Field(default=0, ge=0, le=86400000)
    platform: Literal["ios", "android", "unknown"] = "unknown"
    model: str = Field(default="", max_length=80)
    system: str = Field(default="", max_length=80)
    wechatVersion: str = Field(default="", max_length=40)
    sdkVersion: str = Field(default="", max_length=40)
    appVersion: str = Field(default="", max_length=40)


def install_payment_diagnostics(app, ledger, current_session):
    @app.post("/api/rent-room/orders/request/{request_id}/diagnostics")
    async def report(request_id: str, body: PaymentDiagnostic, session=Depends(current_session)):
        order = ledger.order(session["account"], request_id)
        if not order or session["provider"] != "wechat":
            raise HTTPException(404)
        data = body.model_dump(exclude={"eventId"})
        for key, value in data.items():
            if isinstance(value, str):
                data[key] = safe_text(value, secrets=(order["openid"],))
        try:
            added = ledger.save_payment_event(order["id"], body.eventId, json.dumps(data, ensure_ascii=False))
        except ValueError as error:
            raise HTTPException(429, detail="Payment diagnostic limit reached") from error
        if added:
            payment_log("client_report", order, clientEvent=body.eventId,
                        clientStage=data["stage"], **{k: v for k, v in data.items() if k != "stage"})
        return {"receipt": body.eventId}
