import asyncio
import base64
import hashlib
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .rent_room_settings import RentRoomSettings
from .rent_room_store import RentRoomStore
from .rent_room_wechat import SecurePush, WeChatUnavailable, WeChatVirtualPay, compact


PRODUCT = {"id": "house-capacity-10", "price": 190, "size": 10}


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=256)


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestId: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    productId: str = Field(pattern=r"^house-capacity-10$")
    platform: str = Field(default="unknown", pattern=r"^(ios|android|unknown)$")


def create_app(settings=None, *, provider=None, store=None):
    settings = settings or RentRoomSettings()
    ledger = store or RentRoomStore(settings.db_path)
    wechat = provider or WeChatVirtualPay(settings)
    cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(
        settings.token_secret.get_secret_value().encode()).digest())) if settings.token_secret else None
    push = SecurePush(settings) if settings.push_token and settings.push_aes_key else None
    account_locks = defaultdict(asyncio.Lock)
    login_limits = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(_):
        yield
        await wechat.close()
        ledger.close()

    app = FastAPI(title="Rent-room Capacity API", lifespan=lifespan)

    @app.middleware("http")
    async def private_responses(request, call_next):
        # The service accepts tiny login/order/push payloads, never house media.
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > 65536:
            return Response(status_code=413)
        response = await call_next(request)
        response.headers["cache-control"] = "no-store"
        response.headers["x-content-type-options"] = "nosniff"
        return response

    def unavailable(message="房源包暂未开放购买，请稍后再试"):
        return HTTPException(503, detail={"code": "RENT_ROOM_UNAVAILABLE", "message": message})

    @app.exception_handler(WeChatUnavailable)
    async def upstream_error(request, error):
        # Do not expose URLs containing WeChat credentials or session keys.
        return Response(compact({"detail": {"code": "WECHAT_UNAVAILABLE", "message": str(error)}}),
                        status_code=503, media_type="application/json")

    async def current_session(authorization: str | None = Header(default=None)):
        if not settings.login_ready:
            raise unavailable()
        session = ledger.session(authorization[7:]) if authorization and authorization.startswith("Bearer ") else None
        if not session:
            raise HTTPException(401, detail={"code": "RENT_ROOM_UNAUTHORIZED", "message": "请重新登录微信后重试"})
        try:
            cipher.decrypt(session["session_key"].encode())
        except InvalidToken as error:
            raise HTTPException(401, detail={"message": "微信登录已过期，请重试"}) from error
        return session

    def entitlement(account):
        packs = ledger.packs(account)
        return {"packs": packs, "limit": 2 + 10 * packs}

    async def reconcile(order, *, acknowledge_delivery=True):
        if order["state"] == "refunded":
            return "refunded"
        result = await wechat.query(order)
        if result.get("order_id") != order["id"] or result.get("order_type") not in (0, 7):
            raise WeChatUnavailable("微信订单信息不一致，请稍后重试")
        status = result.get("status")
        if status in (2, 4):
            if result.get("paid_fee") != PRODUCT["price"] or not result.get("wx_order_id"):
                raise WeChatUnavailable("微信订单金额不一致，请稍后重试")
            try:
                ledger.apply(order["id"], "paid", result["wx_order_id"])
            except sqlite3.IntegrityError as error:
                raise WeChatUnavailable("该支付订单已关联其他记录，请联系支持") from error
            if status == 2 and acknowledge_delivery:
                # Persist fulfillment before ACK; retrying never grants twice.
                await wechat.delivered(order)
        elif status in (5, 8):
            ledger.apply(order["id"], "refunded")
        elif status == 6:
            ledger.apply(order["id"], "closed")
        elif status in (0, 1):
            ledger.apply(order["id"], "unpaid")
        elif status == 3:
            ledger.apply(order["id"], "pending")
        elif status != 7 or order["state"] != "paid":
            raise WeChatUnavailable("微信订单状态暂时无法确认，请稍后重试")
        return ledger.by_id(order["id"])["state"]

    @app.get("/health/live")
    async def health():
        return {"status": "ok"}

    @app.get("/api/rent-room/catalog")
    async def catalog():
        return {"available": settings.payment_ready, "iosAvailable": settings.payment_ready and settings.ios_payments_enabled,
                "product": PRODUCT}

    @app.post("/api/rent-room/session")
    async def login(body: LoginRequest, request: Request):
        if not settings.login_ready:
            raise unavailable()
        now = time.monotonic()
        # This route is behind Caddy. Use its last-hop client address, never an
        # arbitrary first X-Forwarded-For value supplied by the caller.
        address = request.headers.get("x-forwarded-for", "").split(",")[-1].strip() or (request.client.host if request.client else "unknown")
        if address not in login_limits and len(login_limits) >= 10000:
            login_limits.clear()
        attempts = login_limits[address]
        while attempts and attempts[0] < now - 300:
            attempts.popleft()
        if len(attempts) >= 100:
            raise HTTPException(429, detail={"message": "登录过于频繁，请稍后重试"})
        attempts.append(now)
        result = await wechat.login(body.code)
        account = hashlib.sha256((settings.app_id + ":" + result["openid"]).encode()).hexdigest()
        token = ledger.create_session(account, result["openid"], cipher.encrypt(result["session_key"].encode()).decode())
        return {"account": account, "token": token, **entitlement(account)}

    @app.get("/api/rent-room/entitlements")
    async def entitlements(session=Depends(current_session)):
        async with account_locks[session["account"]]:
            if settings.payment_configured:
                for order in ledger.orders(session["account"]):
                    try:
                        await reconcile(order)
                    except WeChatUnavailable:
                        if order["state"] not in ("unpaid", "pending"):
                            raise
                        ledger.checked(order["id"])
            return entitlement(session["account"])

    @app.post("/api/rent-room/orders")
    async def create_order(body: OrderRequest, session=Depends(current_session)):
        if not settings.payment_ready or body.platform == "ios" and not settings.ios_payments_enabled:
            raise unavailable()
        async with account_locks[session["account"]]:
            try:
                order = ledger.create_order(session, body.requestId)
            except ValueError as error:
                raise HTTPException(429, detail={"message": str(error)}) from error
            response = {"orderId": order["id"], "status": order["state"], **entitlement(session["account"])}
            if order["state"] == "unpaid":
                response["payment"] = wechat.payment(order, cipher.decrypt(session["session_key"].encode()).decode())
            return response

    @app.get("/api/rent-room/orders/request/{request_id}")
    async def order_status(request_id: str, session=Depends(current_session)):
        async with account_locks[session["account"]]:
            order = ledger.order(session["account"], request_id)
            if not order:
                return {"status": "missing", **entitlement(session["account"])}
            if not settings.payment_configured:
                raise unavailable("购买记录暂时无法确认，请稍后重试")
            state = await reconcile(order)
            return {"status": state, **entitlement(session["account"])}

    @app.get("/api/rent-room/wechat/notify")
    async def verify_push(request: Request):
        if not push:
            raise unavailable()
        try:
            push.verify(request.query_params)
        except ValueError as error:
            raise HTTPException(403) from error
        return Response(request.query_params.get("echostr", ""), media_type="text/plain")

    @app.post("/api/rent-room/wechat/notify")
    async def notify(request: Request):
        if not push or not settings.payment_configured:
            raise unavailable()
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413)
        try:
            envelope, is_json = push.parse(bytes(body))
            event, _ = push.decrypt(envelope["Encrypt"], request.query_params)
            if not isinstance(event, dict):
                raise ValueError("Invalid event")
        except Exception as error:
            raise HTTPException(403, detail="Invalid encrypted WeChat notification") from error
        kind = event.get("Event")
        if kind not in ("xpay_goods_deliver_notify", "xpay_refund_notify"):
            return Response("success")
        order = ledger.by_id(event.get("OutTradeNo") or event.get("MchOrderId") or "")
        if not order:
            raise HTTPException(404)
        async with account_locks[order["account"]]:
            # Secure notifications trigger authoritative queries. Their body
            # alone never grants capacity or treats a failed refund as success.
            await reconcile(order, acknowledge_delivery=False)
        plaintext = compact({"ErrCode": 0, "ErrMsg": "success"}).encode() if is_json else b"<xml><ErrCode>0</ErrCode><ErrMsg>success</ErrMsg></xml>"
        reply = push.encrypt(plaintext)
        if is_json:
            return reply
        xml = "<xml>" + "".join(f"<{key}>{value}</{key}>" for key, value in reply.items()) + "</xml>"
        return Response(xml, media_type="application/xml")

    return app


app = create_app()
