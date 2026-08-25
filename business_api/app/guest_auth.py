import base64
import hashlib
import hmac
import json
import time


class GuestTokenError(Exception):
    pass


class GuestTokenCodec:
    def __init__(self, secret: str, ttl_seconds: int) -> None:
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = ttl_seconds

    def issue(self, device_id: str, now: float | None = None) -> str:
        issued_at = int(time.time() if now is None else now)
        subject = self.device_key(device_id)
        payload = {
            "aud": "time-fragment-ios",
            "exp": issued_at + self._ttl_seconds,
            "sub": subject,
            "v": 1,
        }
        encoded = self._encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        signature = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        return f"{encoded}.{self._encode(signature)}"

    def verify(self, token: str, now: float | None = None) -> str:
        try:
            encoded, supplied_signature = token.split(".", 1)
            expected_signature = self._encode(
                hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise GuestTokenError("invalid guest token")
            payload = json.loads(self._decode(encoded))
            expires_at = payload["exp"]
            subject = payload["sub"]
        except (GuestTokenError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GuestTokenError("invalid guest token") from error

        current_time = int(time.time() if now is None else now)
        if (
            payload.get("aud") != "time-fragment-ios"
            or payload.get("v") != 1
            or not isinstance(expires_at, int)
            or expires_at <= current_time
            or not isinstance(subject, str)
            or not subject.startswith("guest_")
        ):
            raise GuestTokenError("invalid or expired guest token")
        return subject

    @staticmethod
    def device_key(device_id: str) -> str:
        return "guest_" + hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)
