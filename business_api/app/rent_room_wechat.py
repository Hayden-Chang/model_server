import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import struct
import time
from xml.etree import ElementTree as ET

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)


class WeChatUnavailable(Exception):
    pass


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class WeChatVirtualPay:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.http = httpx.AsyncClient(base_url="https://api.weixin.qq.com", timeout=10, transport=transport)
        self.access_token = ""
        self.token_expires = 0
        self.token_lock = asyncio.Lock()

    async def close(self):
        await self.http.aclose()

    async def _get(self, path, params):
        try:
            response = await self.http.get(path, params=params)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("errcode", 0) != 0:
                raise WeChatUnavailable("微信服务暂时不可用，请稍后重试")
            return data
        except (httpx.HTTPError, ValueError) as error:
            raise WeChatUnavailable("微信服务暂时不可用，请稍后重试") from error

    async def login(self, code):
        data = await self._get("/sns/jscode2session", {
            "appid": self.settings.app_id, "secret": self.settings.app_secret.get_secret_value(),
            "js_code": code, "grant_type": "authorization_code"})
        if not data.get("openid") or not data.get("session_key"):
            raise WeChatUnavailable("微信登录未完成，请重试")
        return data

    async def _token(self, *, rejected=None):
        async with self.token_lock:
            if rejected is not None and self.access_token == rejected:
                self.token_expires = 0
            if time.time() >= self.token_expires:
                data = await self._get("/cgi-bin/token", {
                    "grant_type": "client_credential", "appid": self.settings.app_id,
                    "secret": self.settings.app_secret.get_secret_value()})
                self.access_token = data["access_token"]
                self.token_expires = time.time() + max(0, data["expires_in"] - 120)
            return self.access_token

    def signature(self, message):
        return hmac.new(self.settings.app_key.get_secret_value().encode(), message.encode(), hashlib.sha256).hexdigest()

    def payment(self, order, session_key):
        data = compact({"offerId": self.settings.offer_id, "buyQuantity": 1, "env": 0,
                        "currencyType": "CNY", "productId": order["product_id"], "goodsPrice": order["price"],
                        "outTradeNo": order["id"], "attach": order["id"]})
        return {"mode": "short_series_goods", "signData": data,
                "paySig": self.signature("requestVirtualPayment&" + data),
                "signature": hmac.new(session_key.encode(), data.encode(), hashlib.sha256).hexdigest()}

    async def _post(self, path, order):
        body = compact({"openid": order["openid"], "env": 0, "order_id": order["id"]})
        try:
            token = await self._token()
            for attempt in range(2):
                response = await self.http.post(path, params={"access_token": token,
                    "pay_sig": self.signature(path + "&" + body)}, content=body,
                    headers={"content-type": "application/json"})
                response.raise_for_status()
                data = response.json()
                code = data.get("errcode", 0) if isinstance(data, dict) else None
                if code in (40001, 40014, 42001) and attempt == 0:
                    logger.warning("WeChat payment %s rejected token (%s); refreshing once", path, code)
                    token = await self._token(rejected=token)
                    continue
                if code != 0:
                    logger.warning("WeChat payment %s failed (%s)", path, code)
                    raise WeChatUnavailable("微信订单暂时无法确认，请稍后重试")
                return data
        except (httpx.HTTPError, ValueError, KeyError) as error:
            logger.warning("WeChat payment %s failed (%s)", path, type(error).__name__)
            raise WeChatUnavailable("微信订单暂时无法确认，请稍后重试") from error

    async def query(self, order):
        result = (await self._post("/xpay/query_order", order)).get("order", {})
        if not isinstance(result, dict):
            raise WeChatUnavailable("微信订单暂时无法确认，请稍后重试")
        return result

    async def delivered(self, order):
        await self._post("/xpay/notify_provide_goods", order)


class SecurePush:
    def __init__(self, settings):
        self.app_id = settings.app_id.encode()
        self.token = settings.push_token.get_secret_value()
        self.key = base64.b64decode(settings.push_aes_key.get_secret_value() + "=", validate=True)

    def signature(self, timestamp, nonce, encrypted=""):
        return hashlib.sha1("".join(sorted([self.token, timestamp, nonce] + ([encrypted] if encrypted else []))).encode()).hexdigest()

    def verify(self, query, encrypted=""):
        timestamp, nonce = query.get("timestamp", ""), query.get("nonce", "")
        signature = query.get("msg_signature" if encrypted else "signature", "")
        if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > 300 or not 1 <= len(nonce) <= 128:
            raise ValueError("Invalid push timestamp")
        if not hmac.compare_digest(signature, self.signature(timestamp, nonce, encrypted)):
            raise ValueError("Invalid push signature")

    @staticmethod
    def parse(body):
        if body.lstrip().startswith(b"{"):
            return json.loads(body), True
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise ValueError("Unsafe XML")
        def convert(node):
            return {child.tag: convert(child) for child in node} if len(node) else node.text or ""
        return convert(ET.fromstring(body)), False

    def decrypt(self, encrypted, query):
        self.verify(query, encrypted)
        decoder = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
        data = decoder.update(base64.b64decode(encrypted, validate=True)) + decoder.finalize()
        padding = data[-1]
        if not 1 <= padding <= 32 or data[-padding:] != bytes([padding]) * padding:
            raise ValueError("Invalid push padding")
        data = data[:-padding]
        length = struct.unpack("!I", data[16:20])[0]
        if data[20 + length:] != self.app_id:
            raise ValueError("Invalid push recipient")
        return self.parse(data[20:20 + length])

    def encrypt(self, body):
        data = secrets.token_bytes(16) + struct.pack("!I", len(body)) + body + self.app_id
        padding = 32 - len(data) % 32
        data += bytes([padding]) * padding
        encoder = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).encryptor()
        encrypted = base64.b64encode(encoder.update(data) + encoder.finalize()).decode()
        timestamp, nonce = str(int(time.time())), secrets.token_hex(8)
        return {"Encrypt": encrypted, "MsgSignature": self.signature(timestamp, nonce, encrypted),
                "TimeStamp": timestamp, "Nonce": nonce}
