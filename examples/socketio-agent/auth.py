"""A login hook for the example agent: /signin, then /verifyotp, then refresh tokens.

This is what `agent.auth.hook` points at. The shape of the exchange is the one the
target systems use - POST credentials, POST a one-time code, get an access token and
a refresh token - and `CachedTokenAuth` supplies the rest: the cache, the expiry
check, the single-flight lock, and a fresh token when the adapter reports a refusal.

The one-time code comes from config here because the example server has a fixed
one. Against a real system it is whatever your test identity can read: a fixed code
on a test tenant, a mailbox it can poll, a TOTP secret. That part is yours to
write; nothing else in the adapter knows OTP exists.

Needs `requests`, which the `socketio` extra brings with python-socketio.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import requests

from askback.adapters.socketio_adapter import CachedTokenAuth, Token
from askback.core.errors import AgentCallError


class OTPAuth(CachedTokenAuth):
    def __init__(
        self,
        *,
        base_url: str,
        users: Mapping[str, Mapping[str, str]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(clock=clock)
        self._now = clock
        self._base = base_url.rstrip("/")
        self._users = users
        self._refresh_tokens: dict[str, str] = {}

    def login(self, principal: str) -> Token:
        user = self._users.get(principal)
        if user is None:
            raise AgentCallError(f"auth hook knows no user for principal {principal!r}")
        self._post("/signin", {"username": user["username"], "password": user["password"]})
        return self._token(principal, self._post("/verifyotp", {"username": user["username"], "otp": user["otp"]}))

    def refresh(self, principal: str, stale: Token) -> Token:
        refresh_token = self._refresh_tokens.get(principal)
        if refresh_token is None:
            return self.login(principal)
        return self._token(principal, self._post("/refresh", {"refresh_token": refresh_token}))

    def _token(self, principal: str, body: Mapping[str, Any]) -> Token:
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise AgentCallError("sign-in answered without an access_token")
        refresh = body.get("refresh_token")
        if isinstance(refresh, str):
            self._refresh_tokens[principal] = refresh
        ttl = body.get("expires_in")
        expires = self._now() + float(ttl) if isinstance(ttl, (int, float)) else None
        return Token(access, expires)

    def _post(self, path: str, payload: Mapping[str, str]) -> dict[str, Any]:
        try:
            response = requests.post(self._base + path, json=dict(payload), timeout=10)
        except requests.RequestException as exc:
            raise AgentCallError(f"{self._base}{path} unreachable: {type(exc).__name__}") from exc
        if not response.ok:
            # Never the body: it may echo what we sent.
            raise AgentCallError(f"{self._base}{path} answered HTTP {response.status_code}")
        body = response.json()
        if not isinstance(body, dict):
            raise AgentCallError(f"{self._base}{path} did not answer with a JSON object")
        return body


def factory(**options: Any) -> OTPAuth:
    return OTPAuth(**options)
