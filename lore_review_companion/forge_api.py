# SPDX-License-Identifier: GPL-2.0-or-later

"""Small Gitea REST client with retry-safe read operations."""

from __future__ import annotations

import ipaddress
import json
import os
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class HTTPResponse(Protocol):
    status: int
    headers: Mapping[str, str]

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


Transport = Callable[[Request, float], HTTPResponse]
MAX_JSON_BYTES = 8 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _urlopen_transport(request: Request, timeout: float) -> HTTPResponse:
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


class ForgeAPIError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


def read_private_token(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or not 1 <= info.st_size <= 4096:
                raise ValueError("Token file must be a private regular file owned by this user")
            raw = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
        token = raw.decode("ascii").strip()
    except (OSError, UnicodeError) as error:
        raise ValueError("Token file could not be read safely") from error
    if not token or len(raw) > 4096 or any(ord(character) < 33 or ord(character) == 127 for character in token):
        raise ValueError("Token file contains an invalid credential")
    return token


class ForgeAPI:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 10,
        max_retries: int = 2,
        retry_delay: float = 0.25,
        transport: Transport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        origin = urlsplit(base_url)
        if (
            origin.scheme not in ("http", "https") or not origin.hostname
            or origin.username is not None or origin.password is not None
            or origin.query or origin.fragment
        ):
            raise ValueError("Gitea API base URL must be a credential-free HTTP(S) URL")
        if token and origin.scheme == "http":
            try:
                is_loopback = ipaddress.ip_address(origin.hostname).is_loopback
            except ValueError:
                is_loopback = origin.hostname.lower() == "localhost"
            if not is_loopback:
                raise ValueError("Gitea API credentials require HTTPS except on loopback")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if retry_delay < 0:
            raise ValueError("retry_delay cannot be negative")
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.transport = transport or _urlopen_transport
        self.sleeper = sleeper

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        status, response_body = self._request(path, method=method, params=params, payload=payload, max_bytes=MAX_JSON_BYTES)
        if response_body is None:
            return None
        try:
            return json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ForgeAPIError(status, "Gitea returned invalid JSON") from error

    def request_bytes(
        self, path: str, *, params: Mapping[str, Any] | None = None,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> bytes:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        _, response_body = self._request(path, params=params, accept="text/plain", max_bytes=max_bytes)
        return response_body or b""

    def _request(
        self, path: str, *, method: str = "GET", params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None, accept: str = "application/json",
        max_bytes: int | None = None,
    ) -> tuple[int, bytes | None]:
        parts = urlsplit(path)
        if (
            parts.scheme or parts.netloc or parts.query or parts.fragment
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise ValueError("Gitea API path must be a relative path without a query or fragment")
        for segment in parts.path.lstrip("/").split("/"):
            decoded = segment
            while True:
                previous = decoded
                decoded = unquote(decoded)
                if decoded == previous:
                    break
            if decoded in (".", "..") or "/" in decoded or "\\" in decoded:
                raise ValueError("Gitea API path contains an unsafe segment")
        url = urljoin(self.base_url, parts.path.lstrip("/"))
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": accept}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"token {self.token}"

        retryable = method.upper() == "GET"
        attempt = 0
        while True:
            request = Request(url, data=body, headers=headers, method=method.upper())
            try:
                try:
                    response = self.transport(request, self.timeout)
                    try:
                        status = response.status
                        response_body = response.read(max_bytes + 1) if max_bytes is not None else response.read()
                        retry_after = response.headers.get("Retry-After")
                    finally:
                        response.close()
                except HTTPError as error:
                    try:
                        status = error.code
                        response_body = error.read(max_bytes + 1) if max_bytes is not None else error.read()
                        retry_after = error.headers.get("Retry-After") if error.headers else None
                    finally:
                        error.close()
            except (URLError, OSError, EOFError, HTTPException) as error:
                if retryable and attempt < self.max_retries:
                    self._wait(attempt, None)
                    attempt += 1
                    continue
                detail = error.reason if isinstance(error, URLError) else type(error).__name__
                raise ForgeAPIError(None, f"Gitea request failed: {detail}") from error

            if 200 <= status < 300:
                if max_bytes is not None and len(response_body) > max_bytes:
                    raise ForgeAPIError(status, f"Gitea response exceeds {max_bytes} bytes")
                if status == 204 or not response_body:
                    return status, None
                return status, response_body

            if retryable and status in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                self._wait(attempt, retry_after)
                attempt += 1
                continue
            raise ForgeAPIError(status, f"Gitea returned HTTP {status}")

    def get_pages(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        per_page: int = 50,
        max_pages: int = 1000,
    ) -> Iterator[Any]:
        if per_page < 1 or per_page > 100:
            raise ValueError("per_page must be between 1 and 100")
        if not 1 <= max_pages <= 1000:
            raise ValueError("max_pages must be between 1 and 1000")
        page = 1
        while True:
            page_params = dict(params or {})
            page_params.update({"page": page, "limit": per_page})
            result = self.request_json(path, params=page_params)
            if not isinstance(result, list):
                raise ForgeAPIError(200, "Expected a paginated JSON array")
            yield from result
            if len(result) < per_page:
                return
            if page >= max_pages:
                raise ForgeAPIError(200, "Gitea pagination exceeds the page limit")
            page += 1

    def _wait(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                delay = min(max(float(retry_after), 0), 5)
            except ValueError:
                delay = self.retry_delay * (2**attempt)
        else:
            delay = self.retry_delay * (2**attempt)
        if delay:
            self.sleeper(delay)


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: int
    login: str


def authenticated_user(api: ForgeAPI) -> AuthenticatedUser:
    user = api.request_json("user")
    login = user.get("login") if isinstance(user, dict) else None
    user_id = user.get("id") if isinstance(user, dict) else None
    if (
        not isinstance(login, str) or not login.strip()
        or not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 1
    ):
        raise ValueError("Gitea did not identify the authenticated account")
    return AuthenticatedUser(user_id, login)


def authenticated_login(api: ForgeAPI) -> str:
    return authenticated_user(api).login