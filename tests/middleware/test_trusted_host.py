from __future__ import annotations

import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Host, Route, Router, WebSocketRoute
from starlette.testclient import WebSocketDenialResponse
from starlette.types import Message, Receive, Scope, Send
from starlette.websockets import WebSocket
from tests.types import TestClientFactory


def test_trusted_host_middleware(test_client_factory: TestClientFactory) -> None:
    def homepage(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK", status_code=200)

    app = Starlette(
        routes=[Route("/", endpoint=homepage)],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["testserver", "*.testserver"])],
    )

    client = test_client_factory(app)
    response = client.get("/")
    assert response.status_code == 200

    client = test_client_factory(app, base_url="http://subdomain.testserver")
    response = client.get("/")
    assert response.status_code == 200

    client = test_client_factory(app, base_url="http://invalidhost")
    response = client.get("/")
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("host", "status_code"),
    [
        ("[::1]", 200),
        ("[::1]:8000", 200),
        ("[v1.foo]", 200),
        ("[:::]", 400),
        ("[::ffff:999.999.999.999]", 400),
        ("[2001:db8::1]", 400),
        ("[::1", 400),
        ("[::1]evil.example.com", 400),
        ("[::1]@attacker", 400),
        ("[::1].", 400),
        ("[::1]:evil", 400),
        ("[::1]:", 400),
        ("api_service", 200),
        ("api_service:000080", 200),
        ("api_service:65535", 200),
        ("api_service:65536", 200),
        ("api_service:100000", 200),
        ("api_service:8000", 200),
        ("api_service/evil", 400),
        ("api_service?evil", 400),
        ("api_service#evil", 400),
        ("api_service@attacker", 400),
        ("api service", 400),
    ],
)
def test_trusted_host_middleware_ipv6(test_client_factory: TestClientFactory, host: str, status_code: int) -> None:
    def homepage(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    app = Starlette(
        routes=[Route("/", endpoint=homepage)],
        middleware=[
            Middleware(TrustedHostMiddleware, allowed_hosts=["[::1]", "[v1.foo]", "api_service", "*.example.com"])
        ],
    )

    client = test_client_factory(app)
    response = client.get("/", headers={"host": host})
    assert response.status_code == status_code


def test_default_allowed_hosts() -> None:
    app = Starlette()
    middleware = TrustedHostMiddleware(app)
    assert middleware.allowed_hosts == ["*"]


def test_www_redirect(test_client_factory: TestClientFactory) -> None:
    def homepage(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK", status_code=200)

    app = Starlette(
        routes=[Route("/", endpoint=homepage)],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["www.example.com"])],
    )

    client = test_client_factory(app, base_url="https://example.com")
    response = client.get("/")
    assert response.status_code == 200
    assert response.url == "https://www.example.com/"


def test_trusted_host_middleware_is_case_insensitive(test_client_factory: TestClientFactory) -> None:
    app = TrustedHostMiddleware(PlainTextResponse("OK"), allowed_hosts=["Example.com"])

    response = test_client_factory(app).get("/", headers={"host": "example.COM"})
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        [],  # Missing Host header.
        [(b"host", b"example.com"), (b"host", b"example.com")],  # Duplicate Host headers.
        [(b"host", b"example.com"), (b"host", b"attacker.com")],
        [(b"host", b"example.com\x01")],  # Control characters.
        [(b"host", b"example.com\x7f")],
        [(b"host", b"user@example.com")],  # Userinfo is not allowed.
        [(b"host", b"user:pass@example.com")],
        [(b"host", b"example.com:99999")],  # Invalid port.
        [(b"host", b"example.com:port")],
        [(b"host", b"[v1.foo]")],  # Ambiguous IPvFuture literal.
        [(b"host", b"[:::]")],  # Invalid IPv6 literal.
    ],
)
async def test_strict_mode_rejects_invalid_raw_authority(headers: list[tuple[bytes, bytes]]) -> None:
    app_called = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal app_called
        app_called = True

    middleware = TrustedHostMiddleware(app, allowed_hosts=["example.com", "[::1]", "[v1.foo]"], strict=True)
    scope: Scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        messages.append(message)

    await middleware(scope, receive, send)
    assert not app_called
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 400


@pytest.mark.anyio
async def test_strict_mode_websocket_rejects_invalid_raw_authority() -> None:
    app_called = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal app_called
        app_called = True

    middleware = TrustedHostMiddleware(app, allowed_hosts=["example.com"], strict=True)
    scope: Scope = {
        "type": "websocket",
        "scheme": "ws",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"example.com"), (b"host", b"attacker.com")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        messages.append(message)

    await middleware(scope, receive, send)
    assert not app_called
    assert messages[0]["status"] == 400


@pytest.mark.parametrize(
    ("allowed_hosts", "scheme", "host", "status_code"),
    [
        (["example.com:8080"], "http", "example.com:8080", 200),
        (["example.com:8080"], "http", "example.com:08080", 200),  # Leading zeros are normalized.
        (["example.com:8080"], "http", "example.com", 400),  # Omitted port is the default port, not 8080.
        (["example.com:8080"], "http", "example.com:80", 400),
        (["example.com:8080"], "https", "example.com:8080", 200),  # Explicit ports match on any scheme.
        (["example.com"], "http", "example.com", 200),
        (["example.com"], "http", "example.com:80", 200),  # Omitted and default ports are equivalent.
        (["example.com"], "http", "example.com:443", 400),
        (["example.com"], "https", "example.com:443", 200),
        (["example.com"], "https", "example.com:80", 400),
        (["example.com"], "https", "example.com", 200),
        (["*.example.com:8443"], "https", "sub.example.com:8443", 200),
        (["*.example.com:8443"], "https", "sub.example.com", 400),
        (["*.example.com:8443"], "https", "sub.example.com:443", 400),
    ],
)
def test_strict_mode_port_constraints(
    test_client_factory: TestClientFactory, allowed_hosts: list[str], scheme: str, host: str, status_code: int
) -> None:
    app = TrustedHostMiddleware(PlainTextResponse("OK"), allowed_hosts=allowed_hosts, strict=True)

    client = test_client_factory(app, base_url=f"{scheme}://example.com")
    response = client.get("/", headers={"host": host})
    assert response.status_code == status_code


def test_strict_mode_lenient_mode_unchanged(test_client_factory: TestClientFactory) -> None:
    # The default lenient mode still tolerates out-of-range ports and IPvFuture literals.
    app = TrustedHostMiddleware(PlainTextResponse("OK"), allowed_hosts=["example.com", "[v1.foo]"])

    client = test_client_factory(app)
    assert client.get("/", headers={"host": "example.com:100000"}).status_code == 200
    assert client.get("/", headers={"host": "[v1.foo]"}).status_code == 200


def test_strict_mode_websocket(test_client_factory: TestClientFactory) -> None:
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("OK")
        await websocket.close()

    app = Starlette(
        routes=[WebSocketRoute("/", endpoint=websocket_endpoint)],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["testserver:8000"], strict=True)],
    )

    client = test_client_factory(app)
    with client.websocket_connect("/", headers={"host": "testserver:8000"}) as websocket:
        assert websocket.receive_text() == "OK"

    with pytest.raises(WebSocketDenialResponse) as exc_info:
        with client.websocket_connect("/", headers={"host": "testserver:8001"}):
            pass  # pragma: no cover
    assert exc_info.value.status_code == 400


def test_strict_mode_www_redirect_uses_validated_authority(test_client_factory: TestClientFactory) -> None:
    def homepage(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK", status_code=200)

    app = Starlette(
        routes=[Route("/", endpoint=homepage)],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["www.example.com:8443"], strict=True)],
    )

    client = test_client_factory(app, base_url="https://example.com:8443", follow_redirects=False)
    response = client.get("/")
    assert response.status_code == 307
    assert response.headers["location"] == "https://www.example.com:8443/"


def test_strict_mode_consistent_scope_interpretation(test_client_factory: TestClientFactory) -> None:
    # The same scope passes host validation, `Host` route matching, and URL
    # generation, and all three interpret the authority identically.
    def homepage(request: Request) -> PlainTextResponse:
        assert request.base_url.netloc == "api.example.com:8000"
        return PlainTextResponse(str(request.url_for("homepage")))

    app = Starlette(
        routes=[Host("api.example.com", app=Router(routes=[Route("/", endpoint=homepage, name="homepage")]))],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["api.example.com:8000"], strict=True)],
    )

    client = test_client_factory(app)
    response = client.get("/", headers={"host": "api.example.com:8000"})
    assert response.status_code == 200
    assert response.text == "http://api.example.com/"


def test_strict_mode_url_generation_uses_validated_authority(test_client_factory: TestClientFactory) -> None:
    def homepage(request: Request) -> PlainTextResponse:
        return PlainTextResponse(str(request.url_for("homepage")))

    app = Starlette(
        routes=[Route("/", endpoint=homepage, name="homepage")],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["api.example.com:8000"], strict=True)],
    )

    client = test_client_factory(app)
    response = client.get("/", headers={"host": "api.example.com:8000"})
    assert response.status_code == 200
    assert response.text == "http://api.example.com:8000/"


def test_strict_mode_ipv6_url_generation(test_client_factory: TestClientFactory) -> None:
    def homepage(request: Request) -> PlainTextResponse:
        return PlainTextResponse(str(request.url_for("homepage")))

    app = Starlette(
        routes=[Route("/", endpoint=homepage, name="homepage")],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["[::1]:8000"], strict=True)],
    )

    client = test_client_factory(app)
    response = client.get("/", headers={"host": "[::1]:8000"})
    assert response.status_code == 200
    assert response.text == "http://[::1]:8000/"
