import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509.oid import NameOID

from app.appstore_client import (
    APPLE_SANDBOX_URL,
    AppStoreEnvironmentMismatch,
    AppStoreRejected,
    AppStoreServerAPIClient,
    AppStoreUnavailable,
    JWSVerificationFailed,
    build_client_token,
    verify_apple_jws,
)

NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)
KEY_ID = "H26PU75Z9S"
ISSUER_ID = "b24fefbf-30af-43f3-8523-8f2e5c8f8c74"
BUNDLE_ID = "com.hayden.timefragment"


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(subject: x509.Name, issuer_name: x509.Name, public_key, signing_key,
                 not_before: datetime, not_after: datetime) -> x509.Certificate:
    return (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer_name)
            .public_key(public_key).serial_number(x509.random_serial_number())
            .not_valid_before(not_before).not_valid_after(not_after)
            .sign(signing_key, hashes.SHA256()))


@dataclass(frozen=True)
class CertChain:
    root_key: ec.EllipticCurvePrivateKey
    root_certificate: x509.Certificate
    leaf_key: ec.EllipticCurvePrivateKey
    leaf_certificate: x509.Certificate


@pytest.fixture(scope="module")
def certs():
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_certificate = _certificate(_name("Test Root"), _name("Test Root"),
                                    root_key.public_key(), root_key,
                                    NOW - timedelta(days=365), NOW + timedelta(days=365))
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_certificate = _certificate(_name("Apple Test Signer"), root_certificate.subject,
                                    leaf_key.public_key(), root_key,
                                    NOW - timedelta(days=1), NOW + timedelta(days=1))
    return CertChain(root_key=root_key, root_certificate=root_certificate,
                     leaf_key=leaf_key, leaf_certificate=leaf_certificate)


def _variant_leaf(certs: CertChain, *, not_before=None, not_after=None, issuer_name=None):
    return _certificate(_name("Apple Test Signer"),
                        issuer_name or certs.root_certificate.subject,
                        certs.leaf_key.public_key(), certs.root_key,
                        not_before or NOW - timedelta(days=1),
                        not_after or NOW + timedelta(days=1))


def _der_b64(certificate: x509.Certificate) -> str:
    return base64.b64encode(certificate.public_bytes(Encoding.DER)).decode()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _build_jws(payload: dict, certs: CertChain, leaf_certificate, *,
               x5c: list[str] | None = None, alg: str = "ES256") -> str:
    header = {"alg": alg, "x5c": x5c if x5c is not None else [
        _der_b64(leaf_certificate), _der_b64(certs.root_certificate)]}
    head = _b64(json.dumps(header).encode())
    body = _b64(json.dumps(payload).encode())
    if alg != "ES256":
        return head + "." + body + "." + _b64(b"not-a-real-signature")
    der_signature = certs.leaf_key.sign((head + "." + body).encode(), ec.ECDSA(hashes.SHA256()))
    r_value, s_value = decode_dss_signature(der_signature)
    raw_signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
    return head + "." + body + "." + _b64(raw_signature)


def _key_p8() -> bytes:
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def _make_client(certs: CertChain, *, environment="sandbox", handler=None):
    def default_handler(request: httpx.Request) -> httpx.Response:
        payload = {"environment": "Sandbox" if environment == "sandbox" else "Production",
                   "originalTransactionId": "123"}
        return httpx.Response(200, json={"signedPayload": _build_jws(
            payload, certs, certs.leaf_certificate)})
    return AppStoreServerAPIClient(
        environment=environment, key_p8=_key_p8(), key_id=KEY_ID, issuer_id=ISSUER_ID,
        bundle_id=BUNDLE_ID, http=httpx.AsyncClient(transport=httpx.MockTransport(handler or default_handler)),
        pinned_roots=[certs.root_certificate])


def test_valid_jws_verifies_and_returns_payload(certs):
    token = _build_jws({"transactionId": "txn-1"}, certs, certs.leaf_certificate)
    decoded = verify_apple_jws(token, now=NOW, pinned_roots=[certs.root_certificate])
    assert decoded["transactionId"] == "txn-1"


def test_tampered_payload_fails(certs):
    token = _build_jws({"transactionId": "txn-1"}, certs, certs.leaf_certificate)
    head, _, signature = token.split(".")
    other = _b64(json.dumps({"transactionId": "txn-2"}).encode())
    with pytest.raises(JWSVerificationFailed, match="BAD_SIGNATURE"):
        verify_apple_jws(head + "." + other + "." + signature, now=NOW,
                         pinned_roots=[certs.root_certificate])


def test_unsupported_algorithm_fails(certs):
    token = _build_jws({"a": 1}, certs, certs.leaf_certificate, alg="RS256")
    with pytest.raises(JWSVerificationFailed, match="UNSUPPORTED_ALG"):
        verify_apple_jws(token, now=NOW, pinned_roots=[certs.root_certificate])


def test_missing_chain_fails(certs):
    token = _build_jws({"a": 1}, certs, certs.leaf_certificate, x5c=[])
    with pytest.raises(JWSVerificationFailed, match="MISSING_CERTIFICATE_CHAIN"):
        verify_apple_jws(token, now=NOW, pinned_roots=[certs.root_certificate])


def test_untrusted_root_fails(certs):
    foreign_key = ec.generate_private_key(ec.SECP256R1())
    foreign_root = _certificate(_name("Foreign Root"), _name("Foreign Root"),
                                foreign_key.public_key(), foreign_key,
                                NOW - timedelta(days=1), NOW + timedelta(days=1))
    foreign_leaf = _certificate(_name("Apple Test Signer"), foreign_root.subject,
                                certs.leaf_key.public_key(), foreign_key,
                                NOW - timedelta(days=1), NOW + timedelta(days=1))
    token = _build_jws({"a": 1}, certs, foreign_leaf,
                       x5c=[_der_b64(foreign_leaf), _der_b64(foreign_root)])
    with pytest.raises(JWSVerificationFailed, match="UNTRUSTED_ROOT"):
        verify_apple_jws(token, now=NOW, pinned_roots=[certs.root_certificate])


def test_expired_leaf_fails(certs):
    expired_leaf = _variant_leaf(certs, not_before=NOW - timedelta(days=3),
                                 not_after=NOW - timedelta(days=1))
    token = _build_jws({"a": 1}, certs, expired_leaf)
    with pytest.raises(JWSVerificationFailed, match="CERTIFICATE_EXPIRED"):
        verify_apple_jws(token, now=NOW, pinned_roots=[certs.root_certificate])


def test_broken_chain_fails(certs):
    broken_leaf = _variant_leaf(certs, issuer_name=_name("Not The Signer"))
    token = _build_jws({"a": 1}, certs, broken_leaf)
    with pytest.raises(JWSVerificationFailed, match="BROKEN_CHAIN"):
        verify_apple_jws(token, now=NOW, pinned_roots=[certs.root_certificate])


def test_malformed_jws_fails(certs):
    with pytest.raises(JWSVerificationFailed, match="MALFORMED_JWS"):
        verify_apple_jws("only.two", now=NOW, pinned_roots=[certs.root_certificate])


def test_client_token_claims_and_shape():
    token = build_client_token(key_p8=_key_p8(), key_id=KEY_ID, issuer_id=ISSUER_ID,
                               bundle_id=BUNDLE_ID, now=NOW)
    head, body, signature = token.split(".")
    header = json.loads(base64.urlsafe_b64decode(head + "=="))
    payload = json.loads(base64.urlsafe_b64decode(body + "=="))
    assert header == {"alg": "ES256", "kid": KEY_ID, "typ": "JWT"}
    assert payload["iss"] == ISSUER_ID
    assert payload["aud"] == "appstoreconnect-v1"
    assert payload["bid"] == BUNDLE_ID
    assert payload["exp"] - payload["iat"] == 300
    assert len(base64.urlsafe_b64decode(signature + "==")) == 64


def test_client_rejects_unknown_environment():
    with pytest.raises(ValueError):
        AppStoreServerAPIClient(environment="staging", key_p8=_key_p8(), key_id="k",
                                issuer_id="i", bundle_id=BUNDLE_ID)


def test_subscription_status_verifies_and_returns_payload(certs):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        payload = {"environment": "Sandbox", "originalTransactionId": "123"}
        return httpx.Response(200, json={"signedPayload": _build_jws(payload, certs, certs.leaf_certificate)})

    client = _make_client(certs, handler=handler)

    async def run():
        try:
            return await client.subscription_status("123")
        finally:
            await client.aclose()

    payload = asyncio.run(run())
    assert payload["originalTransactionId"] == "123"
    assert seen["url"].startswith(APPLE_SANDBOX_URL + "/inApps/v1/subscriptions/123")
    assert seen["authorization"].startswith("Bearer ")


def test_subscription_status_detects_environment_mismatch(certs):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"environment": "Sandbox", "originalTransactionId": "123"}
        return httpx.Response(200, json={"signedPayload": _build_jws(payload, certs, certs.leaf_certificate)})

    client = _make_client(certs, environment="production", handler=handler)

    async def run():
        try:
            await client.subscription_status("123")
        finally:
            await client.aclose()

    with pytest.raises(AppStoreEnvironmentMismatch):
        asyncio.run(run())


def test_subscription_status_maps_transport_error_to_unavailable(certs):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = AppStoreServerAPIClient(
        environment="sandbox", key_p8=_key_p8(), key_id=KEY_ID, issuer_id=ISSUER_ID,
        bundle_id=BUNDLE_ID, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        pinned_roots=[certs.root_certificate])

    async def run():
        try:
            await client.subscription_status("123")
        finally:
            await client.aclose()

    with pytest.raises(AppStoreUnavailable):
        asyncio.run(run())


def test_subscription_status_maps_status_codes(certs):
    rejected = _make_client(certs, handler=lambda request: httpx.Response(404, json={}))

    async def run_rejected():
        try:
            await rejected.subscription_status("123")
        finally:
            await rejected.aclose()

    with pytest.raises(AppStoreRejected) as rejected_error:
        asyncio.run(run_rejected())
    assert rejected_error.value.status_code == 404

    unavailable = _make_client(certs, handler=lambda request: httpx.Response(503, json={}))

    async def run_unavailable():
        try:
            await unavailable.subscription_status("123")
        finally:
            await unavailable.aclose()

    with pytest.raises(AppStoreUnavailable):
        asyncio.run(run_unavailable())
