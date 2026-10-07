"""Own public-client OAuth registration; never reads Codex credential files."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
import os
import fcntl
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import jwt
import keyring

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
SERVICE = "GoalKeeper Harness OAuth"


class AuthError(RuntimeError):
    pass


class AuthBroker:
    def __init__(self, private: Path, http: httpx.AsyncClient | None = None, vault=None):
        self.private = private
        self.private.mkdir(parents=True, exist_ok=True)
        self.private.chmod(0o700)
        self.metadata_path = private / "account.json"
        self.registration_path = private / "registration.json"
        self.host_path = private / "host-id"
        if not self.host_path.exists():
            self.host_path.write_text(f"urn:uuid:{uuid4()}")
            self.host_path.chmod(0o600)
        self.host_id = self.host_path.read_text().strip()
        # Authentication runs outside task tools and follows the user's configured system network.
        # Task subprocesses still receive a cleaned environment and their own Seatbelt restrictions.
        self.http = http or httpx.AsyncClient(timeout=30, trust_env=True, follow_redirects=False)
        self.vault = vault or keyring
        self.pending: dict[str, dict] = {}
        self.lock = asyncio.Lock()
        self.last_error: str | None = None
        self.last_diagnostic: dict | None = None

    def metadata(self) -> dict:
        if not self.metadata_path.exists():
            return {}
        return json.loads(self.metadata_path.read_text())

    def status(self) -> dict:
        meta = self.metadata()
        return {
            "connected": bool(meta),
            "email": meta.get("email"),
            "plan_usage_enabled": "chatgpt.tokens.use.direct" in meta.get("scopes", []),
            "pending": any(p["expires"] > time.time() for p in self.pending.values()),
            "error": self.last_error,
            "diagnostic": self.last_diagnostic,
        }

    def registered_client(self) -> str | None:
        if not self.registration_path.exists():
            return None
        record = json.loads(self.registration_path.read_text())
        if record.get("issuer") != ISSUER or record.get("host_id") != self.host_id:
            return None
        return record.get("client_id")

    def retain_registration(self, client_id: str) -> None:
        # An issued public client ID is a registration, not proof of a signed-in identity.
        # Retain it before code exchange so a failed exchange does not register another app.
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", client_id) or client_id == "dynamic_agent_client":
            raise AuthError("注册回调的 client_id 格式无效。")
        temp = self.registration_path.with_suffix(".tmp")
        temp.write_text(json.dumps({"client_id": client_id, "issuer": ISSUER, "host_id": self.host_id}))
        temp.chmod(0o600)
        temp.replace(self.registration_path)

    def exchange_error(self, response: httpx.Response, stage: str) -> AuthError:
        try:
            body = response.json()
        except ValueError:
            body = None
        code = None
        if isinstance(body, dict):
            error = body.get("error")
            candidate = error.get("code") if isinstance(error, dict) else error
            if isinstance(candidate, str) and re.fullmatch(r"[a-z0-9_]{1,100}", candidate):
                code = candidate
        request_id = response.headers.get("x-request-id")
        if request_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            request_id = None
        self.last_diagnostic = {
            "stage": stage,
            "http_status": response.status_code,
            "error_code": code,
            "body_shape": "json_object" if isinstance(body, dict) else "non_object",
            "request_id": request_id,
        }
        label = "登录凭据换取" if stage == "code_exchange" else "登录凭据续期"
        detail = f"HTTP {response.status_code}" + (f"，{code}" if code else "")
        if code == "unsupported_country_region_territory":
            advice = "服务拒绝了后端请求所在的国家或地区，请核对后端网络环境是否满足 OpenAI 的地区要求。"
        elif code in {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired"}:
            advice = "本次登录凭据已失效，请从工作台首页重新发起授权。"
        elif response.status_code == 403:
            advice = "请核对服务支持范围、账号和应用权限；重复同一授权不能解决访问限制。"
        elif response.status_code >= 500 or response.status_code == 429:
            advice = "授权服务暂时不可用，请稍后从工作台重新开始；已有凭据保持不变。"
        else:
            advice = "请核对授权配置后，从工作台首页重新开始。"
        return AuthError(f"{label}失败（{detail}）。{advice}")

    def begin(self, redirect_uri: str, browser_session: str) -> str:
        if not redirect_uri.startswith("http://127.0.0.1:") or not redirect_uri.endswith("/auth/callback"):
            raise AuthError("授权回调必须使用 127.0.0.1 的 /auth/callback。")
        state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
        meta = self.metadata()
        client_id = meta.get("client_id") or self.registered_client()
        self.last_error = None
        self.last_diagnostic = None
        self.pending = {k: v for k, v in self.pending.items() if v["expires"] > time.time()}
        self.pending[state] = {
            "nonce": nonce,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "subject": meta.get("subject"),
            "browser_session": browser_session,
            "expires": time.time() + 600,
        }
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        params = {
            "client_id": client_id or "dynamic_agent_client",
            "ext_agent_host_id": self.host_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if not client_id:
            params["agent_name_hint"] = "GoalKeeper Harness"
        return f"{ISSUER}/api/accounts/authorize?{urlencode(params)}"

    async def _verify(self, encoded: str, client_id: str, nonce: str | None) -> dict:
        response = await self.http.get(f"{ISSUER}/.well-known/jwks.json")
        response.raise_for_status()
        header = jwt.get_unverified_header(encoded)
        if header.get("alg") not in {"RS256", "ES256"}:
            raise AuthError("ID token 签名算法不受支持。")
        matches = [key for key in response.json()["keys"] if key.get("kid") == header.get("kid")]
        if len(matches) != 1:
            raise AuthError("ID token 签名密钥未找到。")
        key = jwt.PyJWK.from_dict(matches[0]).key
        claims = jwt.decode(
            encoded,
            key,
            algorithms=[header["alg"]],
            audience=client_id,
            issuer=ISSUER,
            leeway=5,
            options={"require": ["sub", "exp", "iat"]},
        )
        if nonce is not None and claims.get("nonce") != nonce:
            raise AuthError("ID token nonce 不匹配。")
        return claims

    def _save(self, client_id: str, claims: dict, tokens: dict) -> None:
        scopes = tokens.get("scope", "").split()
        if "chatgpt.tokens.use.direct" not in scopes or not tokens.get("access_token"):
            raise AuthError("账号尚未授予订阅模型调用权限。")
        account_key = f"{client_id}:{claims['sub']}"
        record = {**tokens, "expires_at": time.time() + int(tokens.get("expires_in", 3600))}
        # Native keyring holds credentials, metadata contains no bearer or refresh tokens.
        self.vault.set_password(SERVICE, account_key, json.dumps(record))
        meta = {
            "client_id": client_id,
            "subject": claims["sub"],
            "email": claims.get("email"),
            "scopes": scopes,
            "account_key": account_key,
            "issuer": ISSUER,
            "chatgpt_account_id": claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id"),
        }
        temp = self.metadata_path.with_suffix(".tmp")
        temp.write_text(json.dumps(meta, ensure_ascii=False))
        temp.chmod(0o600)
        temp.replace(self.metadata_path)
        self.last_error = None
        self.last_diagnostic = None

    async def callback(self, params: dict, browser_session: str) -> None:
        state = params.get("state", "")
        pending = self.pending.get(state)
        if not pending or pending["expires"] < time.time():
            raise AuthError("授权状态无效或已过期，请重新开始。")
        if not secrets.compare_digest(pending["browser_session"], browser_session):
            raise AuthError("授权回调与发起授权的浏览器会话不匹配。")
        self.pending.pop(state)
        if params.get("error"):
            raise AuthError(f"授权未完成：{params['error']}")
        client_id = params.get("client_id") or pending["client_id"]
        if not client_id or client_id == "dynamic_agent_client":
            raise AuthError("注册回调未返回已签发的 client_id。")
        if pending["client_id"] and pending["client_id"] != client_id:
            raise AuthError("回调更换了账号的 client_id，已拒绝。")
        self.retain_registration(client_id)
        response = await self.http.post(
            f"{ISSUER}/api/accounts/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": params.get("code", ""),
                "code_verifier": pending["verifier"],
                "redirect_uri": pending["redirect_uri"],
                "resource": RESOURCE,
            },
        )
        if response.status_code != 200:
            raise self.exchange_error(response, "code_exchange")
        tokens = response.json()
        claims = await self._verify(tokens["id_token"], client_id, pending["nonce"])
        if pending["subject"] and claims["sub"] != pending["subject"]:
            raise AuthError("返回账号与原选定账号不一致。")
        self._save(client_id, claims, tokens)

    async def access_token(self) -> str:
        async with self.lock, self.refresh_file_lock():
            meta = self.metadata()
            if not meta:
                raise AuthError("请先在工作台使用 Continue with ChatGPT 授权 GoalKeeper。")
            raw = self.vault.get_password(SERVICE, meta["account_key"])
            if not raw:
                raise AuthError("系统钥匙串内没有 GoalKeeper 凭据，请重新授权。")
            record = json.loads(raw)
            if record["expires_at"] > time.time() + 60:
                return record["access_token"]
            earliest = record.get("earliest_refresh_at", 0)
            if isinstance(earliest, (int, float)) and earliest > time.time():
                raise AuthError("凭据尚不能续期，请等待授权服务允许续期。")
            response = await self.http.post(
                f"{ISSUER}/api/accounts/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": meta["client_id"],
                    "refresh_token": record["refresh_token"],
                    "resource": RESOURCE,
                },
            )
            if response.status_code != 200:
                raise self.exchange_error(response, "refresh")
            renewed = response.json()
            if not renewed.get("refresh_token"):
                raise AuthError("续期未返回替换 refresh token，原记录保持不变。")
            claims = await self._verify(renewed["id_token"], meta["client_id"], None)
            if claims["sub"] != meta["subject"]:
                raise AuthError("续期账号发生变化，拒绝替换凭据。")
            self._save(meta["client_id"], claims, renewed)
            return renewed["access_token"]

    @asynccontextmanager
    async def refresh_file_lock(self):
        # Four evaluator processes must not concurrently rotate the same single-use refresh token.
        descriptor = os.open(self.private / "refresh.lock", os.O_RDWR | os.O_CREAT, 0o600)
        acquisition = asyncio.create_task(asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_EX))
        try:
            await asyncio.shield(acquisition)
            yield
        finally:
            await asyncio.shield(acquisition)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def logout(self) -> None:
        meta = self.metadata()
        if meta:
            self.vault.delete_password(SERVICE, meta["account_key"])
            self.metadata_path.unlink(missing_ok=True)
        self.pending.clear()

    async def close(self) -> None:
        await self.http.aclose()
