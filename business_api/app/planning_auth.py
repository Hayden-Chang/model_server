"""Short-lived, body-bound credentials used only between the two services."""

import base64
import hashlib
import hmac
import json
import time


def body_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class PlanningCredentials:
    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()

    def issue(
        self,
        principal: str,
        body: dict,
        attempt: str,
        diagnostic_trace_id: str | None = None,
    ) -> str:
        claims = {"aud": "time-fragment-planning", "sub": principal, "exp": int(time.time()) + 60,
                  "body": body_hash(body), "attempt": attempt}
        if diagnostic_trace_id is not None:
            claims["diagnosticTraceID"] = diagnostic_trace_id
        encoded = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True).encode()).decode().rstrip("=")
        return encoded + "." + hmac.new(self._secret, encoded.encode(), hashlib.sha256).hexdigest()

    def issue_diagnostic(self, principal: str, trace_id: str, ttl_seconds: int = 900) -> str:
        claims = {
            "aud": "time-fragment-diagnostic",
            "sub": principal,
            "trace": trace_id,
            "exp": int(time.time()) + ttl_seconds,
        }
        encoded = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True).encode()).decode().rstrip("=")
        return encoded + "." + hmac.new(self._secret, encoded.encode(), hashlib.sha256).hexdigest()

    def verify_diagnostic(self, token: str, principal: str) -> str:
        claims = self._decode(token)
        now = int(time.time())
        if (
            claims.get("aud") != "time-fragment-diagnostic"
            or claims.get("sub") != principal
            or type(claims.get("exp")) is not int
            or not now < claims["exp"] <= now + 900
            or not isinstance(claims.get("trace"), str)
        ):
            raise ValueError("invalid diagnostic credential")
        return claims["trace"]

    def verify(self, token: str, body: dict) -> dict:
        try:
            claims = self._decode(token)
            now = int(time.time())
            if (claims["aud"] != "time-fragment-planning" or type(claims["exp"]) is not int
                    or not now < claims["exp"] <= now + 60 or claims["body"] != body_hash(body)
                    or not isinstance(claims["sub"], str) or not isinstance(claims["attempt"], str)
                    or ("diagnosticTraceID" in claims
                        and not isinstance(claims["diagnosticTraceID"], str))):
                raise ValueError
            return claims
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            raise ValueError("invalid planning credential") from error

    def _decode(self, token: str) -> dict:
        encoded, signature = token.split(".")
        expected = hmac.new(self._secret, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(claims, dict):
            raise ValueError
        return claims
