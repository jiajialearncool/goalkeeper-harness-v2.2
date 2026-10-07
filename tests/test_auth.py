import json
import time
from urllib.parse import parse_qs, urlsplit
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from goalkeeper.auth import AuthBroker, AuthError, ISSUER, SCOPES, SERVICE


class Vault:
    def __init__(self):
        self.values = {}

    def set_password(self, service, key, value):
        self.values[(service, key)] = value

    def get_password(self, service, key):
        return self.values.get((service, key))

    def delete_password(self, service, key):
        self.values.pop((service, key), None)


@pytest.fixture
def oauth(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public["kid"] = "test"
    calls = []
    claims = {
        "iss": ISSUER,
        "sub": "test-sub",
        "aud": "oaiapp_test",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "nonce": "will-set",
    }

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [public]})
        token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})
        return httpx.Response(
            200,
            json={
                "access_token": "synthetic-access",
                "refresh_token": "rotated-refresh",
                "id_token": token,
                "expires_in": 3600,
                "scope": SCOPES,
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    broker = AuthBroker(tmp_path, http=http, vault=Vault())
    url = broker.begin("http://127.0.0.1:8765/auth/callback", "browser")
    query = parse_qs(urlsplit(url).query)
    claims["nonce"] = broker.pending[query["state"][0]]["nonce"]
    return broker, query, claims, calls


async def test_own_oauth_pkce_and_no_codex_credentials(oauth):
    broker, query, _, calls = oauth
    assert query["client_id"] == ["dynamic_agent_client"] and query["code_challenge_method"] == ["S256"]
    assert query["scope"][0] == SCOPES
    await broker.callback(
        {"state": query["state"][0], "code": "synthetic-code", "client_id": "oaiapp_test"}, "browser"
    )
    assert broker.status()["connected"] and await broker.access_token() == "synthetic-access"
    assert "synthetic-access" not in broker.metadata_path.read_text()
    assert len(calls) == 2
    with pytest.raises(AuthError):
        await broker.callback({"state": query["state"][0], "code": "replay"}, "browser")


@pytest.mark.parametrize("failure", ["state", "browser", "nonce", "aud", "issuer", "expired"])
async def test_oauth_rejects_forged_or_expired_callback(oauth, failure):
    broker, query, claims, _ = oauth
    params = {"state": query["state"][0], "code": "synthetic", "client_id": "oaiapp_test"}
    browser = "browser"
    if failure == "state":
        params["state"] = "forged"
    if failure == "browser":
        browser = "wrong-browser"
    if failure == "nonce":
        claims["nonce"] = "wrong"
    if failure == "aud":
        claims["aud"] = "someone-else"
    if failure == "issuer":
        claims["iss"] = "https://wrong.invalid"
    if failure == "expired":
        claims["exp"] = int(time.time()) - 60
    with pytest.raises((AuthError, jwt.PyJWTError)):
        await broker.callback(params, browser)
    assert not broker.status()["connected"] and not broker.vault.values


async def test_refresh_rotation_keeps_identity_and_scope(oauth):
    broker, query, _, calls = oauth
    await broker.callback(
        {"state": query["state"][0], "code": "synthetic", "client_id": "oaiapp_test"}, "browser"
    )
    meta = broker.metadata()
    raw = json.loads(broker.vault.get_password(SERVICE, meta["account_key"]))
    raw["expires_at"] = 0
    raw["refresh_token"] = "old-refresh"
    broker.vault.set_password(SERVICE, meta["account_key"], json.dumps(raw))
    assert await broker.access_token() == "synthetic-access"
    new = json.loads(broker.vault.get_password(SERVICE, meta["account_key"]))
    assert new["refresh_token"] == "rotated-refresh"
    assert "refresh_token=old-refresh" in calls[-2].content.decode()
