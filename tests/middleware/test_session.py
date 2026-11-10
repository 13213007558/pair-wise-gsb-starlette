import json
import re
import typing
from base64 import b64decode, b64encode

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import InMemoryRevocationStore, Session, SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tests.types import TestClientFactory


def view_session(request: Request) -> JSONResponse:
    return JSONResponse({"session": request.session})


async def update_session(request: Request) -> JSONResponse:
    data = await request.json()
    request.session.update(data)
    return JSONResponse({"session": request.session})


async def inplace_update_session(request: Request) -> JSONResponse:
    data = await request.json()
    session = request.session
    session |= data
    return JSONResponse({"session": request.session})


async def popitem_session(request: Request) -> JSONResponse:
    request.session.popitem()
    return JSONResponse({"session": request.session})


async def clear_session(request: Request) -> JSONResponse:
    request.session.clear()
    return JSONResponse({"session": request.session})


async def revoke_session(request: Request) -> JSONResponse:
    request.session.revoke()
    return JSONResponse({"session": request.session})


def no_session_access(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def test_session(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
            Route("/clear_session", endpoint=clear_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example")],
    )
    client = test_client_factory(app)

    response = client.get("/view_session")
    assert response.json() == {"session": {}}

    response = client.post("/update_session", json={"some": "data"})
    assert response.json() == {"session": {"some": "data"}}

    # check cookie max-age
    set_cookie = response.headers["set-cookie"]
    max_age_matches = re.search(r"; Max-Age=([0-9]+);", set_cookie)
    assert max_age_matches is not None
    assert int(max_age_matches[1]) == 14 * 24 * 3600

    response = client.get("/view_session")
    assert response.json() == {"session": {"some": "data"}}

    response = client.post("/clear_session")
    assert response.json() == {"session": {}}

    response = client.get("/view_session")
    assert response.json() == {"session": {}}


@pytest.mark.parametrize("partitioned", [False, True])
def test_partitioned_session(test_client_factory: TestClientFactory, partitioned: bool) -> None:
    app = Starlette(
        routes=[
            Route("/update_session", endpoint=update_session, methods=["POST"]),
            Route("/clear_session", endpoint=clear_session, methods=["POST"]),
        ],
        middleware=[
            Middleware(
                SessionMiddleware,
                secret_key="example",
                https_only=True,
                same_site="none",
                partitioned=partitioned,
            )
        ],
    )
    client = test_client_factory(app, base_url="https://testserver")
    response = client.post("/update_session", json={"some": "data"})
    attributes = response.headers["set-cookie"].split("; ")
    assert ("partitioned" in attributes) is partitioned

    response = client.post("/clear_session")
    attributes = response.headers["set-cookie"].split("; ")
    assert ("partitioned" in attributes) is partitioned


def test_session_expires(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example", max_age=-1)],
    )
    client = test_client_factory(app)

    response = client.post("/update_session", json={"some": "data"})
    assert response.json() == {"session": {"some": "data"}}

    # requests removes expired cookies from response.cookies, we need to
    # fetch session id from the headers and pass it explicitly
    expired_cookie_header = response.headers["set-cookie"]
    expired_session_match = re.search(r"session=([^;]*);", expired_cookie_header)
    assert expired_session_match is not None
    expired_session_value = expired_session_match[1]
    client = test_client_factory(app, cookies={"session": expired_session_value})
    response = client.get("/view_session")
    assert response.json() == {"session": {}}


def test_secure_session(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
            Route("/clear_session", endpoint=clear_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example", https_only=True)],
    )
    secure_client = test_client_factory(app, base_url="https://testserver")
    unsecure_client = test_client_factory(app, base_url="http://testserver")

    response = unsecure_client.get("/view_session")
    assert response.json() == {"session": {}}

    response = unsecure_client.post("/update_session", json={"some": "data"})
    assert response.json() == {"session": {"some": "data"}}

    response = unsecure_client.get("/view_session")
    assert response.json() == {"session": {}}

    response = secure_client.get("/view_session")
    assert response.json() == {"session": {}}

    response = secure_client.post("/update_session", json={"some": "data"})
    assert response.json() == {"session": {"some": "data"}}

    response = secure_client.get("/view_session")
    assert response.json() == {"session": {"some": "data"}}

    response = secure_client.post("/clear_session")
    assert response.json() == {"session": {}}

    response = secure_client.get("/view_session")
    assert response.json() == {"session": {}}


def test_session_cookie_subpath(test_client_factory: TestClientFactory) -> None:
    second_app = Starlette(
        routes=[
            Route("/update_session", endpoint=update_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example", path="/second_app")],
    )
    app = Starlette(routes=[Mount("/second_app", app=second_app)])
    client = test_client_factory(app, base_url="http://testserver/second_app")
    response = client.post("/update_session", json={"some": "data"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    cookie_path_match = re.search(r"; path=(\S+);", cookie)
    assert cookie_path_match is not None
    cookie_path = cookie_path_match.groups()[0]
    assert cookie_path == "/second_app"


def test_invalid_session_cookie(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example")],
    )
    client = test_client_factory(app)

    response = client.post("/update_session", json={"some": "data"})
    assert response.json() == {"session": {"some": "data"}}

    # we expect it to not raise an exception if we provide a bogus session cookie
    client = test_client_factory(app, cookies={"session": "invalid"})
    response = client.get("/view_session")
    assert response.json() == {"session": {}}


def test_session_cookie(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example", max_age=None)],
    )
    client: TestClient = test_client_factory(app)

    response = client.post("/update_session", json={"some": "data"})
    assert response.json() == {"session": {"some": "data"}}

    # check cookie max-age
    set_cookie = response.headers["set-cookie"]
    assert "Max-Age" not in set_cookie

    client.cookies.delete("session")
    response = client.get("/view_session")
    assert response.json() == {"session": {}}


def test_session_zero_max_age(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/update_session", endpoint=update_session, methods=["POST"])],
        middleware=[Middleware(SessionMiddleware, secret_key="example", max_age=0)],
    )
    client = test_client_factory(app)

    response = client.post("/update_session", json={"some": "data"})
    assert "; Max-Age=0; " in response.headers["set-cookie"]


def test_domain_cookie(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example", domain=".example.com")],
    )
    client: TestClient = test_client_factory(app)

    response = client.post("/update_session", json={"some": "data"})
    assert response.json() == {"session": {"some": "data"}}

    # check cookie max-age
    set_cookie = response.headers["set-cookie"]
    assert "domain=.example.com" in set_cookie

    client.cookies.delete("session")
    response = client.get("/view_session")
    assert response.json() == {"session": {}}


def test_set_cookie_only_on_modification(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example")],
    )
    client = test_client_factory(app)

    # Write to session - should send Set-Cookie
    response = client.post("/update_session", json={"some": "data"})
    assert "set-cookie" in response.headers

    # Read-only access - should NOT send Set-Cookie
    response = client.get("/view_session")
    assert response.json() == {"session": {"some": "data"}}
    assert "set-cookie" not in response.headers


def test_vary_cookie_on_access(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
            Route("/no_session", endpoint=no_session_access),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example")],
    )
    client = test_client_factory(app)

    # Modifying session should add Vary: Cookie
    response = client.post("/update_session", json={"some": "data"})
    assert "cookie" in response.headers.get("vary", "").lower()

    # Reading a non-empty session should add Vary: Cookie
    response = client.get("/view_session")
    assert "cookie" in response.headers.get("vary", "").lower()

    # Not accessing session at all should NOT add Vary: Cookie
    response = client.get("/no_session")
    assert "cookie" not in response.headers.get("vary", "").lower()


def test_session_tracks_modification() -> None:
    session = Session({"a": "1", "b": "2"})
    assert not session.modified

    # __setitem__
    session["c"] = "3"
    assert session.modified

    # __delitem__
    session = Session({"a": "1"})
    del session["a"]
    assert session.modified

    # clear
    session = Session({"a": "1"})
    session.clear()
    assert session.modified

    # pop with existing key
    session = Session({"a": "1"})
    session.pop("a")
    assert session.modified
    assert session.accessed

    # pop with missing key
    session = Session({"a": "1"})
    session.pop("missing", None)
    assert not session.modified
    assert not session.accessed

    # setdefault with missing key
    session = Session({"a": "1"})
    session.setdefault("b", "2")
    assert session.modified

    # setdefault with existing key
    session = Session({"a": "1"})
    session.setdefault("a", "2")
    assert not session.modified


@pytest.mark.parametrize(
    ("path", "data", "expected"),
    [
        ("/inplace_update_session", {"c": "3"}, {"a": "1", "b": "2", "c": "3"}),
        ("/popitem_session", None, {"a": "1"}),
    ],
)
def test_session_persists_mutations(
    test_client_factory: TestClientFactory,
    path: str,
    data: dict[str, str] | None,
    expected: dict[str, str],
) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
            Route("/inplace_update_session", endpoint=inplace_update_session, methods=["POST"]),
            Route("/popitem_session", endpoint=popitem_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example")],
    )
    client = test_client_factory(app)

    response = client.post("/update_session", json={"a": "1", "b": "2"})
    assert response.json() == {"session": {"a": "1", "b": "2"}}

    response = client.post(path, json=data)
    assert response.json() == {"session": expected}

    response = client.get("/view_session")
    assert response.json() == {"session": expected}


async def login_app(scope: Scope, receive: Receive, send: Send) -> None:
    scope["session"]["user"] = "alice"
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def logout_app(scope: Scope, receive: Receive, send: Send) -> None:
    scope["session"].revoke()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


class SessionRecorder:
    def __init__(self) -> None:
        self.sessions: list[dict[str, typing.Any]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.sessions.append(dict(scope["session"]))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class PausableReader:
    """ASGI app that loads and modifies the session, then pauses before responding."""

    def __init__(self) -> None:
        self.loaded = anyio.Event()
        self.proceed = anyio.Event()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope["session"]["reader"] = "late-write"
        self.loaded.set()
        await self.proceed.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class FailingRevocationStore:
    """Store whose `is_revoked` starts raising after `fail_after` successful calls."""

    def __init__(self, fail_after: int = 0) -> None:
        self.calls = 0
        self.fail_after = fail_after

    async def is_revoked(self, session_id: str, generation: int) -> bool:
        self.calls += 1
        if self.calls > self.fail_after:
            raise RuntimeError("revocation store unavailable")
        return False

    async def revoke(self, session_id: str, generation: int) -> None:
        raise RuntimeError("revocation store unavailable")


async def run_asgi(app: ASGIApp, cookie: str | None = None) -> list[Message]:
    headers: list[tuple[bytes, bytes]] = []
    if cookie is not None:
        headers.append((b"cookie", f"session={cookie}".encode()))
    scope: Scope = {"type": "http", "method": "GET", "path": "/", "headers": headers}
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await app(scope, receive, send)
    return messages


def session_cookie_values(messages: list[Message]) -> list[str]:
    values: list[str] = []
    for message in messages:
        if message["type"] == "http.response.start":
            for name, value in message.get("headers", []):
                if name.lower() == b"set-cookie":
                    text = value.decode("utf-8")
                    if text.startswith("session="):
                        values.append(text.split(";")[0].removeprefix("session="))
    return values


def session_identity(cookie: str) -> tuple[str, int]:
    payload = json.loads(b64decode(cookie.rsplit(".", 2)[0]))
    return payload["s"], payload["g"]


@pytest.mark.anyio
async def test_in_memory_revocation_store_generations() -> None:
    store = InMemoryRevocationStore()
    assert not await store.is_revoked("session-id", 0)

    await store.revoke("session-id", 1)
    assert await store.is_revoked("session-id", 0)
    assert await store.is_revoked("session-id", 1)
    assert not await store.is_revoked("session-id", 2)

    # Revoking an older generation does not lower the watermark.
    await store.revoke("session-id", 0)
    assert await store.is_revoked("session-id", 1)

    await store.revoke("session-id", 3)
    assert await store.is_revoked("session-id", 3)


@pytest.mark.anyio
async def test_late_response_cannot_reactivate_revoked_session() -> None:
    store = InMemoryRevocationStore()
    reader = PausableReader()
    reader_app = SessionMiddleware(reader, secret_key="example", revocation_store=store)

    login_messages = await run_asgi(SessionMiddleware(login_app, secret_key="example", revocation_store=store))
    (cookie,) = session_cookie_values(login_messages)

    reader_messages: list[Message] = []

    async def run_reader() -> None:
        nonlocal reader_messages
        reader_messages = await run_asgi(reader_app, cookie)

    with anyio.fail_after(5):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_reader)
            await reader.loaded.wait()
            # The logout request completes while the reader is still paused.
            logout_app_ = SessionMiddleware(logout_app, secret_key="example", revocation_store=store)
            logout_messages = await run_asgi(logout_app_, cookie)
            assert session_cookie_values(logout_messages) == ["null"]
            reader.proceed.set()

    # The late response must not write the revoked identity back: it expires
    # the cookie instead of re-delivering a valid old session.
    assert session_cookie_values(reader_messages) == ["null"]

    # The browser presenting the old cookie again is rejected server-side.
    recorder = SessionRecorder()
    await run_asgi(SessionMiddleware(recorder, secret_key="example", revocation_store=store), cookie)
    assert recorder.sessions == [{}]


@pytest.mark.anyio
async def test_session_issued_before_logout_is_rejected_after_logout() -> None:
    store = InMemoryRevocationStore()
    reader = PausableReader()
    reader.proceed.set()  # The reader completes first this time.
    reader_app = SessionMiddleware(reader, secret_key="example", revocation_store=store)

    login_messages = await run_asgi(SessionMiddleware(login_app, secret_key="example", revocation_store=store))
    (cookie,) = session_cookie_values(login_messages)

    reader_messages = await run_asgi(reader_app, cookie)
    (reader_cookie,) = session_cookie_values(reader_messages)
    assert reader_cookie != "null"

    logout_messages = await run_asgi(
        SessionMiddleware(logout_app, secret_key="example", revocation_store=store), cookie
    )
    assert session_cookie_values(logout_messages) == ["null"]

    # The cookie the reader wrote before the logout is revoked as well.
    recorder = SessionRecorder()
    await run_asgi(SessionMiddleware(recorder, secret_key="example", revocation_store=store), reader_cookie)
    assert recorder.sessions == [{}]


@pytest.mark.anyio
async def test_revocation_check_failure_on_load_fails_closed() -> None:
    store = FailingRevocationStore()
    # Logging in does not consult the store, so a cookie can be issued.
    login_messages = await run_asgi(SessionMiddleware(login_app, secret_key="example", revocation_store=store))
    (cookie,) = session_cookie_values(login_messages)

    recorder = SessionRecorder()
    await run_asgi(SessionMiddleware(recorder, secret_key="example", revocation_store=store), cookie)
    assert recorder.sessions == [{}]


@pytest.mark.anyio
async def test_revocation_check_failure_on_write_back_fails_closed() -> None:
    store = FailingRevocationStore(fail_after=1)  # The load check passes, the write-back check raises.
    app = SessionMiddleware(login_app, secret_key="example", revocation_store=store)

    login_messages = await run_asgi(app)
    (cookie,) = session_cookie_values(login_messages)

    messages = await run_asgi(app, cookie)
    # The old identity is not written back when its revocation state is unknown.
    assert session_cookie_values(messages) == ["null"]


@pytest.mark.anyio
async def test_revocation_store_not_consulted_for_empty_session() -> None:
    store = FailingRevocationStore()  # Raises on any query.
    recorder = SessionRecorder()
    messages = await run_asgi(SessionMiddleware(recorder, secret_key="example", revocation_store=store))
    assert recorder.sessions == [{}]
    assert session_cookie_values(messages) == []
    assert store.calls == 0


def test_revoked_cookie_rejected_server_side(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
            Route("/revoke_session", endpoint=revoke_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example", revocation_store=InMemoryRevocationStore())],
    )
    client: TestClient = test_client_factory(app)

    response = client.post("/update_session", json={"some": "data"})
    assert response.json() == {"session": {"some": "data"}}
    old_cookie = client.cookies.get("session")
    assert old_cookie is not None

    response = client.post("/revoke_session")
    assert response.json() == {"session": {}}
    assert "expires=Thu, 01 Jan 1970" in response.headers["set-cookie"]

    response = client.get("/view_session")
    assert response.json() == {"session": {}}

    # Deleting the browser cookie is not the only line of defense: replaying
    # the pre-logout cookie is rejected by the server.
    client.cookies.delete("session")
    client.cookies.set("session", old_cookie)
    response = client.get("/view_session")
    assert response.json() == {"session": {}}


def test_new_login_gets_independent_identity(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
            Route("/revoke_session", endpoint=revoke_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example", revocation_store=InMemoryRevocationStore())],
    )
    client: TestClient = test_client_factory(app)

    client.post("/update_session", json={"user": "alice"})
    first_cookie = client.cookies.get("session")
    assert first_cookie is not None

    client.post("/revoke_session")
    client.post("/update_session", json={"user": "bob"})
    second_cookie = client.cookies.get("session")
    assert second_cookie is not None

    first_id, _ = session_identity(first_cookie)
    second_id, _ = session_identity(second_cookie)
    assert first_id != second_id

    response = client.get("/view_session")
    assert response.json() == {"session": {"user": "bob"}}

    # Revoking the first identity does not affect the second, and the first
    # stays revoked.
    client.cookies.delete("session")
    client.cookies.set("session", first_cookie)
    response = client.get("/view_session")
    assert response.json() == {"session": {}}


def test_revocable_session_expires(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
        ],
        middleware=[
            Middleware(SessionMiddleware, secret_key="example", max_age=-1, revocation_store=InMemoryRevocationStore())
        ],
    )
    client = test_client_factory(app)

    response = client.post("/update_session", json={"some": "data"})
    assert response.json() == {"session": {"some": "data"}}

    expired_cookie_header = response.headers["set-cookie"]
    expired_session_match = re.search(r"session=([^;]*);", expired_cookie_header)
    assert expired_session_match is not None
    client = test_client_factory(app, cookies={"session": expired_session_match[1]})
    response = client.get("/view_session")
    assert response.json() == {"session": {}}


def test_revocable_session_identity_is_signature_protected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example", revocation_store=InMemoryRevocationStore())],
    )
    client: TestClient = test_client_factory(app)

    client.post("/update_session", json={"some": "data"})
    cookie = client.cookies.get("session")
    assert cookie is not None
    payload = json.loads(b64decode(cookie.rsplit(".", 2)[0]))
    assert payload["d"] == {"some": "data"}
    assert payload["g"] == 0
    assert isinstance(payload["s"], str) and payload["s"]

    # Tampering with the embedded identity breaks the signature.
    forged_payload = b64encode(json.dumps({**payload, "s": "forged"}).encode("utf-8")).decode("utf-8")
    client.cookies.delete("session")
    client.cookies.set("session", forged_payload + "." + cookie.split(".", 1)[1])
    response = client.get("/view_session")
    assert response.json() == {"session": {}}


def test_session_without_store_keeps_plain_signed_payload(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/update_session", endpoint=update_session, methods=["POST"])],
        middleware=[Middleware(SessionMiddleware, secret_key="example")],
    )
    client: TestClient = test_client_factory(app)

    client.post("/update_session", json={"some": "data"})
    cookie = client.cookies.get("session")
    assert cookie is not None
    assert json.loads(b64decode(cookie.rsplit(".", 2)[0])) == {"some": "data"}


def test_revoke_without_session_expires_cookie(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/revoke_session", endpoint=revoke_session, methods=["POST"]),
            Route("/clear_session", endpoint=clear_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example", revocation_store=InMemoryRevocationStore())],
    )
    client: TestClient = test_client_factory(app)

    # Explicit logout without a session still expires any cookie the browser holds.
    response = client.post("/revoke_session")
    assert "expires=Thu, 01 Jan 1970" in response.headers["set-cookie"]

    # Clearing an already empty session does not write any cookie.
    response = client.post("/clear_session")
    assert "set-cookie" not in response.headers


def test_clear_session_with_revocation_store(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/view_session", endpoint=view_session),
            Route("/update_session", endpoint=update_session, methods=["POST"]),
            Route("/clear_session", endpoint=clear_session, methods=["POST"]),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key="example", revocation_store=InMemoryRevocationStore())],
    )
    client: TestClient = test_client_factory(app)

    client.post("/update_session", json={"some": "data"})
    response = client.post("/clear_session")
    assert response.json() == {"session": {}}
    assert "expires=Thu, 01 Jan 1970" in response.headers["set-cookie"]

    response = client.get("/view_session")
    assert response.json() == {"session": {}}


@pytest.mark.anyio
async def test_plain_signed_cookie_not_accepted_when_store_enabled() -> None:
    # A cookie issued by the plain signed-cookie mode carries no identity, so
    # it is rejected once a revocation store is configured.
    login_messages = await run_asgi(SessionMiddleware(login_app, secret_key="example"))
    (cookie,) = session_cookie_values(login_messages)

    recorder = SessionRecorder()
    store = InMemoryRevocationStore()
    await run_asgi(SessionMiddleware(recorder, secret_key="example", revocation_store=store), cookie)
    assert recorder.sessions == [{}]
