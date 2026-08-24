import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict, deque
from math import ceil
from threading import Lock
from typing import Callable


class GuestTokenError(Exception):
    pass


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("guest request limit exceeded")
        self.retry_after = retry_after


class GuestTokenCodec:
    def __init__(self, secret: str, ttl_seconds: int) -> None:
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = ttl_seconds

    def issue(self, device_id: str, now: float | None = None) -> str:
        issued_at = int(time.time() if now is None else now)
        subject = "guest_" + hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:24]
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
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)


class GuestRateLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = requests_per_minute
        self._clock = clock
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, subject: str) -> None:
        now = self._clock()
        cutoff = now - 60
        with self._lock:
            requests = self._requests[subject]
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self._limit:
                raise RateLimitExceeded(max(1, ceil(requests[0] + 60 - now)))
            requests.append(now)
