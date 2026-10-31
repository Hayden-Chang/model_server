import asyncio
import base64
import hashlib
import logging
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .rent_room_settings import RentRoomSettings
from .rent_room_apple_auth import install_apple_auth
from .rent_room_product import PRODUCT
from .rent_room_store import RentRoomStore
from .rent_room_credits import install_credit_routes
from .rent_room_wechat import SecurePush, WeChatUnavailable, WeChatVirtualPay, compact


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=256)


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestId: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    productId: str = Field(pattern=r"^room_capacity_10$")
    platform: str = Field(default="unknown", pattern=r"^(ios|android|unknown)$")


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestId: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")
    ease: Literal["方便", "一般", "不方便"] | None = None
    checklist: Literal["合理", "一般", "不合理"] | None = None
    functionality: Literal["目前正常", "遇到问题"] | None = None
    message: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def nonempty(self):
        self.message = self.message.strip()
        if not any((self.ease, self.checklist, self.functionality, self.message)):
            raise ValueError("请至少填写一项反馈")
        return self


def create_app(settings=None, *, provider=None, store=None, reconcile_interval=60, apple=None):
    settings = settings or RentRoomSettings()
    ledger = store or RentRoomStore(settings.db_path)
    wechat = provider or WeChatVirtualPay(settings)
    cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(
        settings.token_secret.get_secret_value().encode()).digest())) if settings.token_secret else None
    push = SecurePush(settings) if settings.push_token and settings.push_aes_key else None
    account_locks = defaultdict(asyncio.Lock)
    login_limits = defaultdict(deque)
    feedback_limits = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(_):
        worker = asyncio.create_task(reconcile_loop()) if settings.payment_configured else None
        try:
            yield
        finally:
            if worker:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            await apple_identity.close()
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
        if not settings.token_secret:
            raise unavailable()
        session = ledger.session(authorization[7:]) if authorization and authorization.startswith("Bearer ") else None
        if not session or session["provider"] not in ("wechat", "apple"):
            raise HTTPException(401, detail={"code": "RENT_ROOM_UNAUTHORIZED", "message": "请重新登录后重试"})
        try:
            if session["provider"] == "wechat":
                cipher.decrypt(session["session_key"].encode())
        except InvalidToken as error:
            raise HTTPException(401, detail={"message": "微信登录已过期，请重试"}) from error
        return session

    def capacity_policy(request):
        if request.headers.get("x-rent-room-capacity-version") in ("2", "3"):
            return {"capacity": {"version": 2, "freeLimit": settings.free_capacity}}
        return {}

    def entitlement(account, request):
        packs = ledger.packs(account)
        # Legacy clients validate the historical two-free-slot response strictly.
        version = request.headers.get("x-rent-room-capacity-version")
        free = settings.free_capacity if version in ("2", "3") else (1 if version == "1" else 2)
        return {"packs": packs, "limit": free + 10 * packs, **capacity_policy(request),
                **({"credits": wallets.snapshot(account)} if version == "3" else {})}

    wallets = install_credit_routes(app, ledger, current_session, settings)
    apple_identity = install_apple_auth(app, settings, ledger, current_session, entitlement, apple=apple)

    async def reconcile(order, *, acknowledge_delivery=True):
        if order["state"] == "refunded":
            return "refunded"
        result = await wechat.query(order)
        if (result.get("order_id") != order["id"] or type(result.get("order_type")) is not int
                or result["order_type"] not in (0, 7)):
            raise WeChatUnavailable("微信订单信息不一致，请稍后重试")
        status = result.get("status")
        if status in (2, 3, 4, 5, 8):
            if (result.get("paid_fee") != order["price"] or result.get("order_fee") != order["price"]
                    or type(result.get("env_type")) is not int or result["env_type"] != 1
                    or not isinstance(result.get("wx_order_id"), str)
                    or not result["wx_order_id"] or order["wx_id"] and result["wx_order_id"] != order["wx_id"]):
                raise WeChatUnavailable("微信订单金额不一致，请稍后重试")
        # Status 3 is already paid and delivering, not an unconfirmed payment.
        if status in (2, 3, 4):
            try:
                ledger.apply(order["id"], "paid", result["wx_order_id"])
            except sqlite3.IntegrityError as error:
                raise WeChatUnavailable("该支付订单已关联其他记录，请联系支持") from error
            if status in (2, 3) and acknowledge_delivery:
                # Persist fulfillment before ACK; retrying never grants twice.
                await wechat.delivered(order)
        elif status in (5, 8):
            # 5 is merchant debit, 8 is user receipt. Only the latter plus a
            # numeric zero remaining amount proves a full completed refund.
            full = status == 8 and type(result.get("left_fee")) is int and result["left_fee"] == 0
            try:
                ledger.apply(order["id"], "refunded" if full else order["state"], result["wx_order_id"])
            except sqlite3.IntegrityError as error:
                raise WeChatUnavailable("该支付订单已关联其他记录，请联系支持") from error
        elif status == 6:
            ledger.apply(order["id"], "closed")
        elif status in (0, 1):
            ledger.apply(order["id"], "unpaid")
        elif status != 7 or order["state"] != "paid":
            raise WeChatUnavailable("微信订单状态暂时无法确认，请稍后重试")
        else:
            ledger.checked(order["id"])
        return ledger.by_id(order["id"])["state"]

    async def reconcile_due(account=None):
        checking = None
        try:
            async with asyncio.timeout(3 if account else 45):
                for candidate in ledger.orders(account, limit=10):
                    async with account_locks[candidate["account"]]:
                        order = ledger.by_id(candidate["id"])
                        checking = order["id"]
                        try:
                            await reconcile(order)
                        except WeChatUnavailable:
                            # A failed upstream call must not starve later orders.
                            ledger.checked(order["id"])
                        checking = None
        except TimeoutError:
            if checking:
                ledger.checked(checking, retry_after=30)

    async def reconcile_loop():
        while True:
            await asyncio.sleep(reconcile_interval)
            try:
                await reconcile_due()
            except Exception:
                # Never log upstream exception text: it can contain credentials.
                logging.getLogger(__name__).warning("Rent-room reconciliation batch failed; will retry")

    @app.get("/health/live")
    async def health():
        return {"status": "ok"}

    @app.get("/api/rent-room/catalog")
    async def catalog(request: Request):
        return {"available": settings.payment_ready, "iosAvailable": settings.payment_ready and settings.ios_payments_enabled,
                "product": PRODUCT, **capacity_policy(request),
                **({"creditVersion": 1} if request.headers.get("x-rent-room-capacity-version") == "3" else {})}

    @app.post("/api/rent-room/feedback", status_code=201)
    async def feedback(body: FeedbackRequest, request: Request):
        # First-use feedback also works before login. Do not collect house
        # content, WeChat identity, or an arbitrary client-supplied account ID.
        now = time.monotonic()
        address = request.headers.get("x-forwarded-for", "").split(",")[-1].strip() or (request.client.host if request.client else "unknown")
        if address not in feedback_limits and len(feedback_limits) >= 10000:
            feedback_limits.clear()
        attempts = feedback_limits[address]
        while attempts and attempts[0] < now - 300:
            attempts.popleft()
        if len(attempts) >= 20:
            raise HTTPException(429, detail={"message": "提交过于频繁，请稍后重试"})
        attempts.append(now)
        try:
            receipt = ledger.save_feedback(body.requestId, compact(body.model_dump(exclude={"requestId"})))
        except ValueError as error:
            raise HTTPException(409, detail={"message": "反馈内容已变化，请重新提交"}) from error
        return {"receipt": receipt}

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
        if settings.payment_configured:
            await reconcile_due(account)
        token = ledger.create_session(account, result["openid"], cipher.encrypt(result["session_key"].encode()).decode())
        return {"account": account, "token": token, **entitlement(account, request)}

    @app.get("/api/rent-room/entitlements")
    async def entitlements(request: Request, session=Depends(current_session)):
        if settings.payment_configured:
            await reconcile_due(session["account"])
        return entitlement(session["account"], request)

    @app.post("/api/rent-room/orders")
    async def create_order(body: OrderRequest, request: Request, session=Depends(current_session)):
        if session["provider"] != "wechat":
            raise unavailable("请使用 iOS 应用内购买扩容")
        if not settings.payment_ready or body.platform == "ios" and not settings.ios_payments_enabled:
            raise unavailable()
        async with account_locks[session["account"]]:
            try:
                kind = "credits" if request.headers.get("x-rent-room-capacity-version") == "3" else "capacity"
                if kind == "credits" and not wallets.snapshot(session["account"])["migrated"]:
                    raise HTTPException(409, detail={"message": "请先确认原有房源点数"})
                order = ledger.create_order(session, body.requestId, kind=kind)
            except ValueError as error:
                raise HTTPException(429, detail={"message": str(error)}) from error
            response = {"orderId": order["id"], "status": order["state"], **entitlement(session["account"], request)}
            if order["state"] == "unpaid":
                response["payment"] = wechat.payment(order, cipher.decrypt(session["session_key"].encode()).decode())
            return response

    @app.get("/api/rent-room/orders/request/{request_id}")
    async def order_status(request_id: str, request: Request, session=Depends(current_session)):
        async with account_locks[session["account"]]:
            order = ledger.order(session["account"], request_id)
            if not order:
                return {"status": "missing", **entitlement(session["account"], request)}
            if not settings.payment_configured:
                raise unavailable("购买记录暂时无法确认，请稍后重试")
            state = await reconcile(order)
            return {"status": state, **entitlement(session["account"], request)}

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
            for field in ("Env", "env"):
                if field in event and str(event[field]) != "0":
                    raise ValueError("Nonproduction notification")
        except Exception as error:
            raise HTTPException(403, detail="Invalid encrypted WeChat notification") from error
        kind = event.get("Event")
        if kind not in ("xpay_goods_deliver_notify", "xpay_refund_notify"):
            return Response("success")
        trade_id = event.get("OutTradeNo") if kind == "xpay_goods_deliver_notify" else event.get("MchOrderId")
        order = ledger.by_id(trade_id or "")
        if not order:
            raise HTTPException(404)
        if event.get("OpenId") != order["openid"]:
            raise HTTPException(409, detail="Notification identity does not match")
        if kind == "xpay_goods_deliver_notify" and str(event.get("Env")) != "0":
            raise HTTPException(403, detail="Missing production environment")
        async with account_locks[order["account"]]:
            # Payments require authoritative queries. Refunds require the
            # authenticated final result, refund IDs and original payment ID.
            if kind == "xpay_refund_notify":
                if not order["wx_id"]:
                    await reconcile(order, acknowledge_delivery=False)
                try:
                    ledger.refund(order["id"], event)
                except (ValueError, sqlite3.IntegrityError) as error:
                    raise HTTPException(409, detail="Refund does not match the payment") from error
            else:
                state = await reconcile(order, acknowledge_delivery=False)
                if state not in ("paid", "refunded"):
                    raise WeChatUnavailable("支付结果尚未确认，请稍后重试")
        plaintext = compact({"ErrCode": 0, "ErrMsg": "success"}).encode() if is_json else b"<xml><ErrCode>0</ErrCode><ErrMsg>success</ErrMsg></xml>"
        reply = push.encrypt(plaintext)
        if is_json:
            return reply
        xml = "<xml>" + "".join(f"<{key}>{value}</{key}>" for key, value in reply.items()) + "</xml>"
        return Response(xml, media_type="application/xml")

    return app


app = create_app()
