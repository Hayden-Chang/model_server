"""App Store Server API client and Apple JWS verification (Phase A2).

The billing routes trust Apple only through this module: every signed
transaction and notification payload is verified against the pinned Apple
root certificate, and subscription status is always re-queried from Apple
with a service credential instead of trusting client claims.
"""

import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.serialization import load_pem_private_key

APPLE_PRODUCTION_URL = "https://api.storekit.itunes.apple.com"
APPLE_SANDBOX_URL = "https://api.storekit.sandbox.itunes.apple.com"
ROOT_CERTIFICATES_PATH = Path(__file__).with_name("apple_root_ca_g3.pem")
AUTH_TOKEN_TTL_SECONDS = 300


class JWSVerificationFailed(Exception):
    """A signed Apple payload failed anchor, chain, signature or validity checks."""


class AppStoreEnvironmentMismatch(Exception):
    """A verified payload belongs to a different store environment."""


class AppStoreRejected(Exception):
    """Apple answered with a definitive client-side rejection (4xx)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"apple store rejected the request: {status_code}")
        self.status_code = status_code


class AppStoreUnavailable(Exception):
    """Apple could not be reached or answered with a retryable server error."""


def _b64url_decode(segment: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (ValueError, TypeError) as error:
        raise JWSVerificationFailed("MALFORMED_BASE64") from error


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _load_pem_certificates(pem: bytes) -> list[x509.Certificate]:
    return x509.load_pem_x509_certificates(pem)


def _default_roots() -> list[x509.Certificate]:
    return _load_pem_certificates(ROOT_CERTIFICATES_PATH.read_bytes())


def _check_validity(certificate: x509.Certificate, now: datetime) -> None:
    if not (certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc):
        raise JWSVerificationFailed("CERTIFICATE_EXPIRED")


def _verify_chain(certificates: list[x509.Certificate], now: datetime,
                  pinned_roots: list[x509.Certificate]) -> x509.Certificate:
    """Verify leaf→root linkage, validity windows and the pinned anchor."""

    if len(certificates) < 2:
        raise JWSVerificationFailed("CHAIN_TOO_SHORT")
    root_fingerprints = {certificate.fingerprint(hashes.SHA256()) for certificate in pinned_roots}
    if certificates[-1].fingerprint(hashes.SHA256()) not in root_fingerprints:
        raise JWSVerificationFailed("UNTRUSTED_ROOT")
    for certificate in certificates:
        _check_validity(certificate, now)
    for certificate, issuer in zip(certificates, certificates[1:]):
        if certificate.issuer != issuer.subject:
            raise JWSVerificationFailed("BROKEN_CHAIN")
        try:
            certificate.verify_directly_issued_by(issuer)
        except Exception as error:
            raise JWSVerificationFailed("BROKEN_CHAIN") from error
    return certificates[0]


def verify_apple_jws(token: str, *, now: datetime | None = None,
                     pinned_roots: list[x509.Certificate] | None = None) -> dict:
    """Verify an Apple signed-object JWS and return its decoded payload."""

    now = now or datetime.now(timezone.utc)
    roots = pinned_roots if pinned_roots is not None else _default_roots()
    parts = token.split(".")
    if len(parts) != 3:
        raise JWSVerificationFailed("MALFORMED_JWS")
    header_b64, payload_b64, signature_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, UnicodeDecodeError) as error:
        raise JWSVerificationFailed("MALFORMED_HEADER") from error
    if header.get("alg") != "ES256":
        raise JWSVerificationFailed("UNSUPPORTED_ALG")
    x5c = header.get("x5c")
    if not isinstance(x5c, list) or not x5c or not all(isinstance(item, str) for item in x5c):
        raise JWSVerificationFailed("MISSING_CERTIFICATE_CHAIN")
    try:
        certificates = [x509.load_der_x509_certificate(_b64url_decode(item)) for item in x5c]
    except (ValueError, TypeError) as error:
        raise JWSVerificationFailed("MALFORMED_CERTIFICATE") from error
    leaf = _verify_chain(certificates, now, roots)
    try:
        public_key = leaf.public_key()
        if not isinstance(public_key, ec.EllipticCurvePublicKey) or public_key.curve.name != "secp256r1":
            raise JWSVerificationFailed("UNSUPPORTED_KEY")
        raw_signature = _b64url_decode(signature_b64)
        if len(raw_signature) != 64:
            raise JWSVerificationFailed("MALFORMED_SIGNATURE")
        der_signature = encode_dss_signature(
            int.from_bytes(raw_signature[:32], "big"), int.from_bytes(raw_signature[32:], "big"))
        public_key.verify(der_signature, (header_b64 + "." + payload_b64).encode(), ec.ECDSA(hashes.SHA256()))
    except JWSVerificationFailed:
        raise
    except Exception as error:
        raise JWSVerificationFailed("BAD_SIGNATURE") from error
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, UnicodeDecodeError) as error:
        raise JWSVerificationFailed("MALFORMED_PAYLOAD") from error
    if not isinstance(payload, dict):
        raise JWSVerificationFailed("MALFORMED_PAYLOAD")
    return payload


def build_client_token(*, key_p8: bytes, key_id: str, issuer_id: str, bundle_id: str,
                       now: datetime | None = None,
                       ttl_seconds: int = AUTH_TOKEN_TTL_SECONDS) -> str:
    """Build the ES256 service credential for App Store Server API calls."""

    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    private_key = load_pem_private_key(key_p8, password=None)
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise ValueError("app store key must be an EC private key")
    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    payload = {"iss": issuer_id, "iat": int(moment.timestamp()),
               "exp": int((moment + timedelta(seconds=ttl_seconds)).timestamp()),
               "aud": "appstoreconnect-v1", "bid": bundle_id}
    header_segment = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_segment = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    der_signature = private_key.sign((header_segment + "." + payload_segment).encode(), ec.ECDSA(hashes.SHA256()))
    r_value, s_value = decode_dss_signature(der_signature)
    raw_signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
    return header_segment + "." + payload_segment + "." + _b64url_encode(raw_signature)


class AppStoreServerAPIClient:
    """Queries authoritative subscription state from Apple for one environment."""

    def __init__(self, *, environment: str, key_p8: bytes, key_id: str, issuer_id: str,
                 bundle_id: str, http: httpx.AsyncClient | None = None,
                 pinned_roots: list[x509.Certificate] | None = None,
                 clock=time.monotonic) -> None:
        if environment not in ("production", "sandbox"):
            raise ValueError("environment must be production or sandbox")
        self.environment = environment
        self.base_url = APPLE_PRODUCTION_URL if environment == "production" else APPLE_SANDBOX_URL
        self.key_p8 = key_p8
        self.key_id = key_id
        self.issuer_id = issuer_id
        self.bundle_id = bundle_id
        self.pinned_roots = pinned_roots
        self.clock = clock
        self.http = http or httpx.AsyncClient(timeout=15)
        self._token: tuple[str, float] | None = None

    async def aclose(self) -> None:
        if self.http:
            await self.http.aclose()

    def _authorize(self) -> str:
        now_mono = self.clock()
        if self._token is None or now_mono - self._token[1] > AUTH_TOKEN_TTL_SECONDS - 30:
            self._token = (build_client_token(key_p8=self.key_p8, key_id=self.key_id,
                                              issuer_id=self.issuer_id, bundle_id=self.bundle_id), now_mono)
        return self._token[0]

    async def subscription_status(self, original_transaction_id: str) -> dict:
        """Fetch and verify the authoritative subscription state for a purchase chain."""

        try:
            response = await self.http.get(
                f"{self.base_url}/inApps/v1/subscriptions/{original_transaction_id}",
                headers={"Authorization": "Bearer " + self._authorize()})
        except httpx.TransportError as error:
            raise AppStoreUnavailable("apple store unreachable") from error
        if response.status_code >= 500 or response.status_code == 429:
            raise AppStoreUnavailable(f"apple store busy: {response.status_code}")
        if response.status_code >= 400:
            raise AppStoreRejected(response.status_code)
        try:
            envelope = response.json()
            signed_payload = envelope["signedPayload"]
            if not isinstance(signed_payload, str):
                raise ValueError
        except (ValueError, KeyError, TypeError) as error:
            raise AppStoreUnavailable("malformed apple status response") from error
        payload = verify_apple_jws(signed_payload, pinned_roots=self.pinned_roots)
        environment = str(payload.get("environment", "")).lower()
        if environment and environment != self.environment:
            raise AppStoreEnvironmentMismatch(environment)
        return payload
