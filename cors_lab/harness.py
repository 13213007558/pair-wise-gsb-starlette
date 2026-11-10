from __future__ import annotations

import functools
from dataclasses import dataclass

import anyio

from starlette.types import ASGIApp, Message, Scope


@dataclass
class ASGIResponse:
    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes
    raised: BaseException | None = None

    def header(self, name: str) -> str | None:
        key = name.lower().encode("latin-1")
        for k, v in self.headers:
            if k == key:
                return v.decode("latin-1")
        return None

    def getlist(self, name: str) -> list[str]:
        key = name.lower().encode("latin-1")
        return [v.decode("latin-1") for k, v in self.headers if k == key]

    @property
    def set_cookie(self) -> list[str]:
        return self.getlist("Set-Cookie")


async def call_asgi(
    app: ASGIApp,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    cookie: str | None = None,
    body: bytes = b"",
) -> ASGIResponse:
    request_headers = list(headers or [])
    if cookie is not None:
        request_headers.append((b"cookie", cookie.encode("latin-1")))

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "server": ("api.example", 443),
        "client": ("10.0.0.9", 51234),
        "root_path": "",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "headers": request_headers,
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    response_start: Message = {}
    raw_headers: list[tuple[bytes, bytes]] = []
    chunks: list[bytes] = []
    raised: BaseException | None = None

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            response_start.update(message)
            raw_headers.extend(message.get("headers", []))
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    try:
        await app(scope, receive, send)
    except BaseException as exc:  # ServerErrorMiddleware re-raises after sending 500.
        raised = exc

    return ASGIResponse(
        status=response_start.get("status", 0),
        headers=raw_headers,
        body=b"".join(chunks),
        raised=raised,
    )


def run(
    app: ASGIApp,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    cookie: str | None = None,
) -> ASGIResponse:
    func = functools.partial(call_asgi, app, method, path, headers=headers, cookie=cookie)
    return anyio.run(func)


def origin_header(value: str) -> list[tuple[bytes, bytes]]:
    return [(b"origin", value.encode("latin-1"))]


def preflight_headers(value: str, requested_method: str = "POST") -> list[tuple[bytes, bytes]]:
    return [
        (b"origin", value.encode("latin-1")),
        (b"access-control-request-method", requested_method.encode("latin-1")),
        (b"access-control-request-headers", b"content-type, x-csrf-token"),
    ]
