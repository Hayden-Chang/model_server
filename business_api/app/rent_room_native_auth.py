"""Native login shares the rent-room session ledger, never house/media storage."""
import asyncio
import hashlib
import hmac
import json
import secrets
import time

import httpx
import jwt
from fastapi import Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field


class NativeAuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


class PhoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(pattern=r"^1[3-9][0-9]{9}$", max_length=11)


class PhoneSessionRequest(PhoneRequest):
    challengeId: str = Field(pattern=r"^[A-Za-z0-9_-]{32,64}$")
    code: str = Field(pattern=r"^[0-9]{6}$", max_length=6)


class AppleSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challengeId: str = Field(pattern=r"^[A-Za-z0-9_-]{32,64}$")
    identityToken: str = Field(min_length=32, max_length=16384)


class AliyunSMS:
    def __init__(self, settings):
        self.settings = settings

    async def send(self, phone, code):
        # Use the official SDK; never automatically retry a chargeable SendSms.
        from alibabacloud_dysmsapi20170525.client import Client
        from alibabacloud_dysmsapi20170525.models import SendSmsRequest
        from alibabacloud_tea_openapi.utils_models import Config
        from darabonba.runtime import RuntimeOptions
        config = Config(access_key_id=self.settings.sms_access_key_id.get_secret_value(),
                        access_key_secret=self.settings.sms_access_key_secret.get_secret_value(),
                        endpoint="dysmsapi.aliyuncs.com", protocol="https", read_timeout=5000, connect_timeout=3000)
        request = SendSmsRequest(phone_numbers=phone, sign_name=self.settings.sms_sign_name,
                                 template_code=self.settings.sms_template_code,
                                 template_param=json.dumps({"code": code}))
        try:
            result = await Client(config).send_sms_with_options_async(request, RuntimeOptions(autoretry=False))
            if result.body.code != "OK":
                raise NativeAuthError("验证码发送失败，请稍后重试", 503)
        except NativeAuthError:
            raise
        except Exception:
            # SDK exception strings can contain signed URLs, credentials or phone numbers.
            raise NativeAuthError("验证码发送失败，请稍后重试", 503) from None


class AppleIdentity:
    def __init__(self, client_id, client=None):
        self.client_id = client_id
        self.client = client or httpx.AsyncClient(timeout=5, follow_redirects=False)
        self.keys, self.refreshed = [], 0
        self.lock = asyncio.Lock()

    async def close(self):
        await self.client.aclose()

    async def verify(self, token):
        try:
            header = jwt.get_unverified_header(token)
            algorithm, kid = header.get("alg"), header.get("kid")
            if algorithm not in ("RS256", "ES256") or not isinstance(kid, str) or len(kid) > 256:
                raise ValueError("Invalid algorithm")
            async with self.lock:
                now = time.monotonic()
                known = any(k.get("kid") == kid for k in self.keys)
                if not self.keys or now - self.refreshed > 3600 or not known and now - self.refreshed > 60:
                    result = await self.client.get("https://appleid.apple.com/auth/keys")
                    result.raise_for_status()
                    payload = result.json()
                    keys = payload.get("keys") if isinstance(payload, dict) else None
                    if not isinstance(keys, list) or len(keys) > 20 or any(not isinstance(k, dict) for k in keys):
                        raise ValueError("Invalid keys")
                    self.keys, self.refreshed = keys, now
                key = next(k for k in self.keys if k.get("kid") == kid and k.get("alg") == algorithm)
            claims = jwt.decode(token, jwt.PyJWK.from_dict(key, algorithm=algorithm).key,
                                algorithms=[algorithm], audience=self.client_id, issuer="https://appleid.apple.com",
                                options={"require": ["sub", "iss", "aud", "exp", "iat", "nonce"]})
            if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 256:
                raise ValueError("Invalid subject")
            if not isinstance(claims["nonce"], str) or not 16 <= len(claims["nonce"]) <= 256:
                raise ValueError("Invalid nonce")
            return claims
        except (httpx.HTTPError, ValueError, KeyError, StopIteration, jwt.PyJWTError):
            raise NativeAuthError("Apple 登录凭证无效或已过期，请重试") from None


class AuthChallenges:
    def __init__(self, ledger):
        self.ledger = ledger
        with ledger.lock, ledger.db:
            ledger.db.executescript("""
                CREATE TABLE IF NOT EXISTS rr_auth_challenges (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL,
                    digest TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, ready INTEGER NOT NULL DEFAULT 0,
                    consumed INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS rr_auth_subject ON rr_auth_challenges(kind,subject,created);
                CREATE TABLE IF NOT EXISTS rr_auth_limits (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ip TEXT NOT NULL, created INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rr_auth_ip ON rr_auth_limits(kind,ip,created);
            """)

    def rate(self, kind, address, limit=60, seconds=300):
        db, now = self.ledger.db, int(time.time())
        with self.ledger.lock, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM rr_auth_limits WHERE created<?", (now - 86400,))
            count = db.execute("SELECT count(*) FROM rr_auth_limits WHERE kind=? AND ip=? AND created>?",
                               (kind, address, now - seconds)).fetchone()[0]
            total = db.execute("SELECT count(*) FROM rr_auth_limits WHERE kind=? AND created>?", (kind, now - seconds)).fetchone()[0]
            if count >= limit or total >= 10000:
                raise NativeAuthError("请求过于频繁，请稍后重试", 429)
            db.execute("INSERT INTO rr_auth_limits(kind,ip,created) VALUES (?,?,?)", (kind, address, now))

    def reserve(self, kind, subject, challenge, digest, daily_limit):
        db, now = self.ledger.db, int(time.time())
        with self.ledger.lock, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM rr_auth_challenges WHERE created<?", (now - 86400,))
            if kind == "phone":
                rows = db.execute("SELECT created FROM rr_auth_challenges WHERE kind=? AND subject=?", (kind, subject)).fetchall()
                total = db.execute("SELECT count(*) FROM rr_auth_challenges WHERE kind=?", (kind,)).fetchone()[0]
                if len(rows) >= 10 or any(row[0] > now - 60 for row in rows) or total >= daily_limit:
                    raise NativeAuthError("验证码请求过于频繁，请稍后重试", 429)
                db.execute("UPDATE rr_auth_challenges SET consumed=1 WHERE kind=? AND subject=?", (kind, subject))
            db.execute("INSERT INTO rr_auth_challenges(id,kind,subject,digest,created,expires,ready) VALUES (?,?,?,?,?,?,?)",
                       (challenge, kind, subject, digest, now, now + 300, int(kind == "apple")))

    def ready(self, challenge):
        with self.ledger.lock, self.ledger.db:
            self.ledger.db.execute("UPDATE rr_auth_challenges SET ready=1 WHERE id=?", (challenge,))

    def consume(self, challenge, kind, subject, digest):
        db, valid = self.ledger.db, False
        with self.ledger.lock, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM rr_auth_challenges WHERE id=? AND kind=?", (challenge, kind)).fetchone()
            if row and row["ready"] and not row["consumed"] and row["attempts"] < 5 and row["expires"] > int(time.time()):
                valid = hmac.compare_digest(row["subject"], subject) and hmac.compare_digest(row["digest"], digest)
                db.execute("UPDATE rr_auth_challenges SET attempts=attempts+1,consumed=? WHERE id=?", (int(valid), challenge))
        if not valid:
            raise NativeAuthError("验证码无效或已过期，请重新获取" if kind == "phone" else "Apple 登录请求已过期，请重试")


def install_native_auth(app, settings, ledger, current_session, entitlement, *, sms=None, apple=None):
    challenges = AuthChallenges(ledger)
    sms = sms or AliyunSMS(settings)
    apple = apple or AppleIdentity(settings.ios_bundle_id)

    @app.exception_handler(NativeAuthError)
    async def auth_error(_, error):
        return Response(json.dumps({"detail": {"code": "RENT_ROOM_AUTH", "message": str(error)}}, ensure_ascii=False),
                        status_code=error.status, media_type="application/json")

    def address(request):
        raw = request.headers.get("x-forwarded-for", "").split(",")[-1].strip() or (request.client.host if request.client else "unknown")
        return hashlib.sha256(raw.encode()).hexdigest()

    def phone_subject(phone):
        return hmac.new(settings.auth_identity_secret.get_secret_value().encode(), ("phone:" + phone).encode(), hashlib.sha256).hexdigest()

    def phone_digest(challenge, subject, code):
        return hmac.new(settings.token_secret.get_secret_value().encode(), f"sms:{challenge}:{subject}:{code}".encode(), hashlib.sha256).hexdigest()

    def session(kind, subject):
        account = hashlib.sha256(f"rent-room:{kind}:{subject}".encode()).hexdigest()
        token = ledger.create_session(account, "", "", provider=kind)
        return {"account": account, "token": token, "provider": kind, "expiresIn": 7200, **entitlement(account)}

    @app.post("/api/rent-room/auth/phone/code")
    async def phone_code(body: PhoneRequest, request: Request):
        if not settings.sms_ready:
            raise NativeAuthError("短信登录服务尚未接通，请稍后再试", 503)
        challenges.rate("sms-send", address(request), limit=20, seconds=600)
        challenge, code = secrets.token_urlsafe(32), f"{secrets.randbelow(1000000):06d}"
        subject = phone_subject(body.phone)
        challenges.reserve("phone", subject, challenge, phone_digest(challenge, subject, code), settings.sms_daily_limit)
        await sms.send(body.phone, code)
        challenges.ready(challenge)
        return {"challengeId": challenge, "expiresIn": 300, "retryAfter": 60}

    @app.post("/api/rent-room/auth/phone/session")
    async def phone_session(body: PhoneSessionRequest, request: Request):
        if not settings.sms_ready:
            raise NativeAuthError("短信登录服务尚未接通，请稍后再试", 503)
        challenges.rate("sms-verify", address(request))
        subject = phone_subject(body.phone)
        challenges.consume(body.challengeId, "phone", subject, phone_digest(body.challengeId, subject, body.code))
        return session("phone", subject)

    @app.post("/api/rent-room/auth/apple/challenge")
    async def apple_challenge(request: Request):
        if not settings.apple_ready:
            raise NativeAuthError("Apple 登录服务尚未接通，请稍后再试", 503)
        challenges.rate("apple-challenge", address(request), limit=20)
        challenge, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        challenges.reserve("apple", "", challenge, hashlib.sha256(nonce.encode()).hexdigest(), 0)
        return {"challengeId": challenge, "nonce": nonce, "expiresIn": 300}

    @app.post("/api/rent-room/auth/apple/session")
    async def apple_session(body: AppleSessionRequest, request: Request):
        if not settings.apple_ready:
            raise NativeAuthError("Apple 登录服务尚未接通，请稍后再试", 503)
        challenges.rate("apple-verify", address(request))
        claims = await apple.verify(body.identityToken)
        challenges.consume(body.challengeId, "apple", "", hashlib.sha256(claims["nonce"].encode()).hexdigest())
        # Email is never used to merge distinct providers or Apple subjects.
        return session("apple", settings.ios_bundle_id + ":" + claims["sub"])

    @app.post("/api/rent-room/session/revoke")
    async def revoke(session=Depends(current_session)):
        with ledger.lock, ledger.db:
            ledger.db.execute("DELETE FROM rr_sessions WHERE token_hash=?", (session["token_hash"],))
        return {"revoked": True}

    return apple
