"""Apple identity verification and one-time challenges for the native app."""
import asyncio
import hashlib
import hmac
import json
import secrets
import time

import httpx
import jwt
from fastapi import Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, ConfigDict, Field


class AppleAuthError(Exception):
    def __init__(self, message="Apple 登录凭证无效或已过期，请重试", status=401):
        super().__init__(message)
        self.status = status


class AppleSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challengeId: str = Field(pattern=r"^[A-Za-z0-9_-]{32,64}$")
    identityToken: str = Field(min_length=32, max_length=16384)


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
            if algorithm != "RS256" or not isinstance(kid, str) or not 1 <= len(kid) <= 256:
                raise ValueError("Invalid signing header")
            async with self.lock:
                now = time.monotonic()
                known = any(k.get("kid") == kid for k in self.keys)
                if not self.keys or now - self.refreshed > 3600 or (not known and now - self.refreshed > 60):
                    result = await self.client.get("https://appleid.apple.com/auth/keys")
                    result.raise_for_status()
                    payload = result.json()
                    keys = payload.get("keys") if isinstance(payload, dict) else None
                    if not isinstance(keys, list) or not 1 <= len(keys) <= 20 or any(not isinstance(k, dict) for k in keys):
                        raise ValueError("Invalid keys")
                    self.keys, self.refreshed = keys, now
                key = next(k for k in self.keys if k.get("kid") == kid and k.get("alg") == "RS256"
                           and k.get("kty") == "RSA" and k.get("use") == "sig")
            claims = jwt.decode(token, jwt.PyJWK.from_dict(key, algorithm="RS256").key,
                                algorithms=["RS256"], audience=self.client_id, issuer="https://appleid.apple.com",
                                options={"strict_aud": True, "require": ["sub", "iss", "aud", "exp", "iat", "nonce"]})
            if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 256:
                raise ValueError("Invalid subject")
            if not isinstance(claims["nonce"], str) or not 16 <= len(claims["nonce"]) <= 256:
                raise ValueError("Invalid nonce")
            if any(type(claims[k]) is not int for k in ("exp", "iat")):
                raise ValueError("Invalid time")
            return claims
        except httpx.HTTPError:
            raise AppleAuthError("Apple 登录验证暂不可用，请稍后重试", 503) from None
        except (ValueError, TypeError, KeyError, StopIteration, jwt.PyJWTError):
            raise AppleAuthError() from None


class AppleChallenges:
    def __init__(self, ledger):
        self.ledger = ledger
        with ledger.lock, ledger.db:
            ledger.db.executescript("""
                CREATE TABLE IF NOT EXISTS rr_apple_challenges (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL, expires INTEGER NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS rr_apple_auth_limits (
                    kind TEXT NOT NULL, ip TEXT NOT NULL, created INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rr_apple_auth_ip ON rr_apple_auth_limits(kind,ip,created);
            """)

    def rate(self, kind, request, limit):
        # Caddy appends the verified peer; do not trust a caller's first hop.
        raw = request.headers.get("x-forwarded-for", "").split(",")[-1].strip() or (request.client.host if request.client else "unknown")
        address = hashlib.sha256(raw.encode()).hexdigest()
        db, now = self.ledger.db, int(time.time())
        with self.ledger.lock, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM rr_apple_auth_limits WHERE created<=?", (now - 300,))
            count = db.execute("SELECT count(*) FROM rr_apple_auth_limits WHERE kind=? AND ip=?", (kind, address)).fetchone()[0]
            total = db.execute("SELECT count(*) FROM rr_apple_auth_limits WHERE kind=?", (kind,)).fetchone()[0]
            if count >= limit or total >= 10000:
                raise AppleAuthError("请求过于频繁，请稍后重试", 429)
            db.execute("INSERT INTO rr_apple_auth_limits VALUES (?,?,?)", (kind, address, now))

    def create(self):
        challenge, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        with self.ledger.lock, self.ledger.db:
            self.ledger.db.execute("DELETE FROM rr_apple_challenges WHERE expires<=?", (now,))
            self.ledger.db.execute("INSERT INTO rr_apple_challenges(id,digest,expires) VALUES (?,?,?)",
                                   (challenge, hashlib.sha256(nonce.encode()).hexdigest(), now + 300))
        return {"challengeId": challenge, "nonce": nonce, "expiresIn": 300}

    def consume(self, challenge, nonce):
        db = self.ledger.db
        with self.ledger.lock, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM rr_apple_challenges WHERE id=?", (challenge,)).fetchone()
            if (not row or row["consumed"] or row["expires"] <= int(time.time())
                    or not hmac.compare_digest(row["digest"], hashlib.sha256(nonce.encode()).hexdigest())):
                raise AppleAuthError("Apple 登录请求已过期，请重试")
            db.execute("UPDATE rr_apple_challenges SET consumed=1 WHERE id=?", (challenge,))


def install_apple_auth(app, settings, ledger, current_session, entitlement, *, apple=None):
    challenges = AppleChallenges(ledger)
    apple = apple or AppleIdentity(settings.ios_bundle_id)

    @app.exception_handler(AppleAuthError)
    async def auth_error(_, error):
        return Response(json.dumps({"detail": {"code": "RENT_ROOM_AUTH", "message": str(error)}}, ensure_ascii=False),
                        status_code=error.status, media_type="application/json")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if request.url.path == "/api/rent-room/auth/apple/session":
            # FastAPI's default includes the rejected input, potentially a token.
            return Response('{"detail":{"code":"RENT_ROOM_AUTH","message":"登录请求无效，请重试"}}',
                            status_code=422, media_type="application/json")
        return await request_validation_exception_handler(request, error)

    def require_ready():
        if not settings.apple_ready:
            raise AppleAuthError("Apple 登录服务尚未接通，请稍后再试", 503)

    @app.post("/api/rent-room/auth/apple/challenge")
    async def challenge(request: Request):
        require_ready()
        challenges.rate("challenge", request, 20)
        return challenges.create()

    @app.post("/api/rent-room/auth/apple/session")
    async def session(body: AppleSessionRequest, request: Request):
        require_ready()
        challenges.rate("verify", request, 60)
        claims = await apple.verify(body.identityToken)
        challenges.consume(body.challengeId, claims["nonce"])
        # Email/name never identify accounts or merge Apple with WeChat users.
        account = hashlib.sha256(f"rent-room:apple:{settings.ios_bundle_id}:{claims['sub']}".encode()).hexdigest()
        token = ledger.create_session(account, "", "", provider="apple")
        return {"account": account, "token": token, "provider": "apple", "expiresIn": 7200,
                **entitlement(account, request)}

    @app.post("/api/rent-room/session/revoke")
    async def revoke(session=Depends(current_session)):
        with ledger.lock, ledger.db:
            ledger.db.execute("DELETE FROM rr_sessions WHERE token_hash=?", (session["token_hash"],))
            ledger.db.execute("DELETE FROM rr_session_providers WHERE token_hash=?", (session["token_hash"],))
        return {"revoked": True}

    return apple
