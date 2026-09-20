from __future__ import annotations

import itertools
import sys
import threading
import time
from asyncio import Task, current_task as asyncio_current_task
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import anyio.lowlevel
import pytest
import sniffio
import trio.lowlevel

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import AppCallTimeoutError, ASGIInstance, TestClient
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect
from tests.types import TestClientFactory


def mock_service_endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"mock": "example"})


mock_service = Starlette(routes=[Route("/", endpoint=mock_service_endpoint)])


def current_task() -> Task[Any] | trio.lowlevel.Task:
    # anyio's TaskInfo comparisons are invalid after their associated native
    # task object is GC'd https://github.com/agronholm/anyio/issues/324
    asynclib_name = sniffio.current_async_library()
    if asynclib_name == "trio":
        return trio.lowlevel.current_task()

    if asynclib_name == "asyncio":
        task = asyncio_current_task()
        if task is None:
            raise RuntimeError("must be called from a running task")  # pragma: no cover
        return task
    raise RuntimeError(f"unsupported asynclib={asynclib_name}")  # pragma: no cover


def startup() -> None:
    raise RuntimeError()


def test_use_testclient_in_endpoint(test_client_factory: TestClientFactory) -> None:
    """
    We should be able to use the test client within applications.

    This is useful if we need to mock out other services,
    during tests or in development.
    """

    def homepage(request: Request) -> JSONResponse:
        client = test_client_factory(mock_service)
        response = client.get("/")
        return JSONResponse(response.json())

    app = Starlette(routes=[Route("/", endpoint=homepage)])

    client = test_client_factory(app)
    response = client.get("/")
    assert response.json() == {"mock": "example"}


def test_testclient_headers_behavior() -> None:
    """
    We should be able to use the test client with user defined headers.

    This is useful if we need to set custom headers for authentication
    during tests or in development.
    """

    client = TestClient(mock_service)
    assert client.headers.get("user-agent") == "testclient"

    client = TestClient(mock_service, headers={"user-agent": "non-default-agent"})
    assert client.headers.get("user-agent") == "non-default-agent"

    client = TestClient(mock_service, headers={"Authentication": "Bearer 123"})
    assert client.headers.get("user-agent") == "testclient"
    assert client.headers.get("Authentication") == "Bearer 123"


def test_use_testclient_as_contextmanager(test_client_factory: TestClientFactory, anyio_backend_name: str) -> None:
    """
    This test asserts a number of properties that are important for an
    app level task_group
    """
    counter = itertools.count()
    identity_runvar = anyio.lowlevel.RunVar[int]("identity_runvar")

    def get_identity() -> int:
        try:
            return identity_runvar.get()
        except LookupError:
            token = next(counter)
            identity_runvar.set(token)
            return token

    startup_task = object()
    startup_loop = None
    shutdown_task = object()
    shutdown_loop = None

    @asynccontextmanager
    async def lifespan_context(app: Starlette) -> AsyncGenerator[None, None]:
        nonlocal startup_task, startup_loop, shutdown_task, shutdown_loop

        startup_task = current_task()
        startup_loop = get_identity()
        async with anyio.create_task_group():
            yield
        shutdown_task = current_task()
        shutdown_loop = get_identity()

    async def loop_id(request: Request) -> JSONResponse:
        return JSONResponse(get_identity())

    app = Starlette(
        lifespan=lifespan_context,
        routes=[Route("/loop_id", endpoint=loop_id)],
    )

    client = test_client_factory(app)

    with client:
        # within a TestClient context every async request runs in the same thread
        assert client.get("/loop_id").json() == 0
        assert client.get("/loop_id").json() == 0

    # that thread is also the same as the lifespan thread
    assert startup_loop == 0
    assert shutdown_loop == 0

    # lifespan events run in the same task, this is important because a task
    # group must be entered and exited in the same task.
    assert startup_task is shutdown_task

    # outside the TestClient context, new requests continue to spawn in new
    # event loops in new threads
    assert client.get("/loop_id").json() == 1
    assert client.get("/loop_id").json() == 2

    first_task = startup_task

    with client:
        # the TestClient context can be re-used, starting a new lifespan task
        # in a new thread
        assert client.get("/loop_id").json() == 3
        assert client.get("/loop_id").json() == 3

    assert startup_loop == 3
    assert shutdown_loop == 3

    # lifespan events still run in the same task, with the context but...
    assert startup_task is shutdown_task

    # ... the second TestClient context creates a new lifespan task.
    assert first_task is not startup_task


def test_error_on_startup(test_client_factory: TestClientFactory) -> None:
    with pytest.deprecated_call(match="The on_startup and on_shutdown parameters are deprecated"):
        startup_error_app = Starlette(on_startup=[startup])

    with pytest.raises(RuntimeError):
        with test_client_factory(startup_error_app):
            pass  # pragma: no cover


def test_exception_in_middleware(test_client_factory: TestClientFactory) -> None:
    class MiddlewareException(Exception):
        pass

    class BrokenMiddleware:
        def __init__(self, app: ASGIApp):
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            raise MiddlewareException()

    broken_middleware = Starlette(middleware=[Middleware(BrokenMiddleware)])

    with pytest.raises(MiddlewareException):
        with test_client_factory(broken_middleware):
            pass  # pragma: no cover


def test_testclient_asgi2(test_client_factory: TestClientFactory) -> None:
    def app(scope: Scope) -> ASGIInstance:
        async def inner(receive: Receive, send: Send) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [[b"content-type", b"text/plain"]],
                }
            )
            await send({"type": "http.response.body", "body": b"Hello, world!"})

        return inner

    client = test_client_factory(app)  # type: ignore
    response = client.get("/")
    assert response.text == "Hello, world!"


def test_testclient_asgi3(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            }
        )
        await send({"type": "http.response.body", "body": b"Hello, world!"})

    client = test_client_factory(app)
    response = client.get("/")
    assert response.text == "Hello, world!"


def test_websocket_blocking_receive(test_client_factory: TestClientFactory) -> None:
    def app(scope: Scope) -> ASGIInstance:
        async def respond(websocket: WebSocket) -> None:
            await websocket.send_json({"message": "test"})

        async def asgi(receive: Receive, send: Send) -> None:
            websocket = WebSocket(scope, receive=receive, send=send)
            await websocket.accept()
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(respond, websocket)
                try:
                    # this will block as the client does not send us data
                    # it should not prevent `respond` from executing though
                    await websocket.receive_json()
                except WebSocketDisconnect:
                    pass

        return asgi

    client = test_client_factory(app)  # type: ignore
    with client.websocket_connect("/") as websocket:
        data = websocket.receive_json()
        assert data == {"message": "test"}


def test_websocket_not_block_on_close(test_client_factory: TestClientFactory) -> None:
    cancelled = False

    def app(scope: Scope) -> ASGIInstance:
        async def asgi(receive: Receive, send: Send) -> None:
            nonlocal cancelled
            try:
                websocket = WebSocket(scope, receive=receive, send=send)
                await websocket.accept()
                await anyio.sleep_forever()
            except anyio.get_cancelled_exc_class():
                cancelled = True
                raise

        return asgi

    client = test_client_factory(app)  # type: ignore
    with client.websocket_connect("/"):
        ...
    assert cancelled


def test_client(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        client = scope.get("client")
        assert client is not None
        host, port = client
        response = JSONResponse({"host": host, "port": port})
        await response(scope, receive, send)

    client = test_client_factory(app)
    response = client.get("/")
    assert response.json() == {"host": "testclient", "port": 50000}


def test_client_custom_client(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        client = scope.get("client")
        assert client is not None
        host, port = client
        response = JSONResponse({"host": host, "port": port})
        await response(scope, receive, send)

    client = test_client_factory(app, client=("192.168.0.1", 3000))
    response = client.get("/")
    assert response.json() == {"host": "192.168.0.1", "port": 3000}


@pytest.mark.parametrize("param", ("2020-07-14T00:00:00+00:00", "España", "voilà"))
def test_query_params(test_client_factory: TestClientFactory, param: str) -> None:
    def homepage(request: Request) -> Response:
        return Response(request.query_params["param"])

    app = Starlette(routes=[Route("/", endpoint=homepage)])
    client = test_client_factory(app)
    response = client.get("/", params={"param": param})
    assert response.text == param


@pytest.mark.parametrize(
    "domain, ok",
    [
        pytest.param(
            "testserver",
            True,
            marks=[
                pytest.mark.xfail(
                    sys.version_info < (3, 11),
                    reason="Fails due to domain handling in http.cookiejar module (see #2152)",
                ),
            ],
        ),
        ("testserver.local", True),
        ("localhost", False),
        ("example.com", False),
    ],
)
def test_domain_restricted_cookies(test_client_factory: TestClientFactory, domain: str, ok: bool) -> None:
    """
    Test that test client discards domain restricted cookies which do not match the
    base_url of the testclient (`http://testserver` by default).

    The domain `testserver.local` works because the Python http.cookiejar module derives
    the "effective domain" by appending `.local` to non-dotted request domains
    in accordance with RFC 2965.
    """

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        response = Response("Hello, world!", media_type="text/plain")
        response.set_cookie(
            "mycookie",
            "myvalue",
            path="/",
            domain=domain,
        )
        await response(scope, receive, send)

    client = test_client_factory(app)
    response = client.get("/")
    cookie_set = len(response.cookies) == 1
    assert cookie_set == ok


def test_forward_follow_redirects(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if "/ok" in scope["path"]:
            response = Response("ok")
        else:
            response = RedirectResponse("/ok")
        await response(scope, receive, send)

    client = test_client_factory(app, follow_redirects=True)
    response = client.get("/")
    assert response.status_code == 200


def test_forward_nofollow_redirects(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        response = RedirectResponse("/ok")
        await response(scope, receive, send)

    client = test_client_factory(app, follow_redirects=False)
    response = client.get("/")
    assert response.status_code == 307


def test_with_duplicate_headers(test_client_factory: TestClientFactory) -> None:
    def homepage(request: Request) -> JSONResponse:
        return JSONResponse({"x-token": request.headers.getlist("x-token")})

    app = Starlette(routes=[Route("/", endpoint=homepage)])
    client = test_client_factory(app)
    response = client.get("/", headers=[("x-token", "foo"), ("x-token", "bar")])
    assert response.json() == {"x-token": ["foo", "bar"]}


def test_merge_url(test_client_factory: TestClientFactory) -> None:
    def homepage(request: Request) -> Response:
        return Response(request.url.path)

    app = Starlette(routes=[Route("/api/v1/bar", endpoint=homepage)])
    client = test_client_factory(app, base_url="http://testserver/api/v1/")
    response = client.get("/bar")
    assert response.text == "/api/v1/bar"


def test_raw_path_with_querystring(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        response = Response(scope.get("raw_path"))
        await response(scope, receive, send)

    client = test_client_factory(app)
    response = client.get("/hello-world", params={"foo": "bar"})
    assert response.content == b"/hello-world"


def test_websocket_raw_path_without_params(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        await websocket.accept()
        raw_path = scope.get("raw_path")
        assert raw_path is not None
        await websocket.send_bytes(raw_path)

    client = test_client_factory(app)
    with client.websocket_connect("/hello-world", params={"foo": "bar"}) as websocket:
        data = websocket.receive_bytes()
        assert data == b"/hello-world"


def test_timeout_deprecation() -> None:
    with pytest.deprecated_call(match="You should not use the 'timeout' argument with the TestClient."):
        client = TestClient(mock_service)
        client.get("/", timeout=1)


def test_app_call_timeout_reclaims_hanging_http_call(test_client_factory: TestClientFactory) -> None:
    """A hanging HTTP call raises AppCallTimeoutError and its app work is cancelled."""
    hang_cancelled = threading.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["path"] == "/hang":
            try:
                await anyio.sleep_forever()
            except anyio.get_cancelled_exc_class():
                hang_cancelled.set()
                raise
        else:
            response = JSONResponse({"ok": True})
            await response(scope, receive, send)

    client = test_client_factory(app)
    with pytest.raises(AppCallTimeoutError, match="did not complete within"):
        client.get("/hang", extensions={"app_call_timeout": 0.2})
    # The app coroutine created by the timed-out call has been reclaimed.
    assert hang_cancelled.wait(5)
    # The client is still usable for subsequent calls.
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_app_call_timeout_client_level_default(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await anyio.sleep_forever()

    client = test_client_factory(app, app_call_timeout=0.2)
    with pytest.raises(AppCallTimeoutError):
        client.get("/")


def test_app_call_timeout_concurrent_http_isolation(test_client_factory: TestClientFactory) -> None:
    """A timed-out call must not affect another concurrent call on the same client."""
    barrier = threading.Barrier(2)
    hang_cancelled = threading.Event()

    async def hang_endpoint(request: Request) -> JSONResponse:
        # Both endpoints rendezvous here, so the two calls are provably
        # in-flight at the same time on the shared portal.
        await anyio.to_thread.run_sync(barrier.wait, 10)
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            hang_cancelled.set()
            raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def ok_endpoint(request: Request) -> JSONResponse:
        await anyio.to_thread.run_sync(barrier.wait, 10)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/hang", hang_endpoint), Route("/ok", ok_endpoint)])

    client = test_client_factory(app)
    results: dict[str, Any] = {}

    def call_hang() -> None:
        try:
            client.get("/hang", extensions={"app_call_timeout": 0.2})
        except AppCallTimeoutError as exc:
            results["hang"] = exc

    def call_ok() -> None:
        results["ok"] = client.get("/ok").status_code

    with client:
        hang_thread = threading.Thread(target=call_hang)
        ok_thread = threading.Thread(target=call_ok)
        hang_thread.start()
        ok_thread.start()
        hang_thread.join(10)
        ok_thread.join(10)

    assert isinstance(results["hang"], AppCallTimeoutError)
    assert results["ok"] == 200
    assert hang_cancelled.wait(5)


def test_app_call_timeout_websocket_handshake(test_client_factory: TestClientFactory) -> None:
    """A WebSocket handshake that is never accepted times out and is reclaimed."""
    app_cancelled = threading.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            app_cancelled.set()
            raise

    client = test_client_factory(app)
    with pytest.raises(AppCallTimeoutError, match="WebSocket handshake did not complete"):
        with client.websocket_connect("/", extensions={"app_call_timeout": 0.2}):
            pass  # pragma: no cover
    # The app task behind the failed handshake has been cancelled.
    assert app_cancelled.wait(5)


def test_app_call_timeout_concurrent_websocket_isolation(test_client_factory: TestClientFactory) -> None:
    """A timed-out WebSocket handshake must not affect a concurrent one."""
    barrier = threading.Barrier(2)
    hang_cancelled = threading.Event()

    async def hang_endpoint(websocket: WebSocket) -> None:
        await anyio.to_thread.run_sync(barrier.wait, 10)
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            hang_cancelled.set()
            raise

    async def ok_endpoint(websocket: WebSocket) -> None:
        await anyio.to_thread.run_sync(barrier.wait, 10)
        await websocket.accept()
        message = await websocket.receive_text()
        await websocket.send_text(message)
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/hang", hang_endpoint), WebSocketRoute("/ok", ok_endpoint)])

    client = test_client_factory(app)
    results: dict[str, Any] = {}

    def connect_hang() -> None:
        try:
            with client.websocket_connect("/hang", extensions={"app_call_timeout": 0.2}):
                pass  # pragma: no cover
        except AppCallTimeoutError as exc:
            results["hang"] = exc

    def connect_ok() -> None:
        with client.websocket_connect("/ok") as websocket:
            websocket.send_text("ping")
            results["ok"] = websocket.receive_text()

    with client:
        hang_thread = threading.Thread(target=connect_hang)
        ok_thread = threading.Thread(target=connect_ok)
        hang_thread.start()
        ok_thread.start()
        hang_thread.join(10)
        ok_thread.join(10)

    assert isinstance(results["hang"], AppCallTimeoutError)
    assert results["ok"] == "ping"
    assert hang_cancelled.wait(5)


def test_websocket_handshake_timeout_is_not_session_lifetime(test_client_factory: TestClientFactory) -> None:
    """The handshake deadline must not silently become the connection's lifetime."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        await websocket.accept()
        # Outlive the handshake timeout before sending the first message.
        await anyio.sleep(0.4)
        await websocket.send_text("hello")
        await websocket.close()

    client = test_client_factory(app)
    with client.websocket_connect("/", extensions={"app_call_timeout": 0.1}) as websocket:
        assert websocket.receive_text() == "hello"


def test_app_call_timeout_does_not_mask_business_exception(test_client_factory: TestClientFactory) -> None:
    """Business exceptions must propagate as-is, never disguised as timeouts."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise ValueError("boom")

    client = test_client_factory(app, app_call_timeout=10)
    with pytest.raises(ValueError, match="boom"):
        client.get("/")


def test_app_call_timeout_with_sync_endpoint_in_thread(test_client_factory: TestClientFactory) -> None:
    """
    Sync code running in a worker thread cannot be force-killed: the caller
    gets AppCallTimeoutError while the thread runs to completion in the
    background, and the client stays usable.
    """
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_endpoint(request: Request) -> JSONResponse:
        started.set()
        release.wait(10)
        finished.set()
        return JSONResponse({"ok": True})

    def quick_endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/blocking", blocking_endpoint), Route("/quick", quick_endpoint)])
    client = test_client_factory(app)
    with client:
        with pytest.raises(AppCallTimeoutError):
            client.get("/blocking", extensions={"app_call_timeout": 0.2})
        # The caller was notified while the sync code is still blocked:
        # the worker thread cannot be killed and keeps running.
        assert started.is_set()
        assert not finished.is_set()
        # Let the abandoned worker thread run to completion.
        release.set()
        assert finished.wait(10)
        # The client and its thread pool remain usable.
        assert client.get("/quick").status_code == 200


def test_app_call_timeout_sync_endpoint_without_context_manager(test_client_factory: TestClientFactory) -> None:
    """
    Without the context manager, the per-call portal is released in the
    background, so the caller gets AppCallTimeoutError promptly even when
    the app is stuck in synchronous code that cannot be force-killed.
    """
    release = threading.Event()
    finished = threading.Event()

    def blocking_endpoint(request: Request) -> JSONResponse:
        release.wait(10)
        finished.set()
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/blocking", blocking_endpoint)])
    client = test_client_factory(app)
    start = time.monotonic()
    with pytest.raises(AppCallTimeoutError):
        client.get("/blocking", extensions={"app_call_timeout": 0.2})
    # The caller was notified promptly, not after the stuck thread finished.
    assert time.monotonic() - start < 5
    assert not finished.is_set()
    release.set()
    assert finished.wait(10)


def test_lifespan_shutdown_runs_once_on_repeated_close(test_client_factory: TestClientFactory) -> None:
    shutdowns = 0

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        nonlocal shutdowns
        yield
        shutdowns += 1

    app = Starlette(lifespan=lifespan)
    client = test_client_factory(app)
    with client:
        pass
    client.__exit__()
    client.__exit__()
    assert shutdowns == 1


def test_lifespan_shutdown_not_run_on_startup_failure(test_client_factory: TestClientFactory) -> None:
    shutdowns = 0

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        nonlocal shutdowns
        raise RuntimeError("startup failure")
        yield  # pragma: no cover
        shutdowns += 1  # pragma: no cover

    app = Starlette(lifespan=lifespan)
    client = test_client_factory(app)
    with pytest.raises(RuntimeError, match="startup failure"):
        with client:
            pass  # pragma: no cover
    assert shutdowns == 0


def test_lifespan_shutdown_once_after_timed_out_calls(test_client_factory: TestClientFactory) -> None:
    """Cancellation of individual calls must not disturb the lifespan task."""
    shutdowns = 0

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        nonlocal shutdowns
        yield
        shutdowns += 1

    async def hang(request: Request) -> JSONResponse:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")  # pragma: no cover

    async def ok(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(
        lifespan=lifespan,
        routes=[Route("/hang", hang), Route("/ok", ok)],
    )
    client = test_client_factory(app)
    with client:
        with pytest.raises(AppCallTimeoutError):
            client.get("/hang", extensions={"app_call_timeout": 0.2})
        assert client.get("/ok").status_code == 200
    assert shutdowns == 1


def test_lifespan_shutdown_once_after_late_app_exception(test_client_factory: TestClientFactory) -> None:
    """An exception raised after the response completed must not cause extra shutdowns."""
    shutdowns = 0

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        nonlocal shutdowns
        yield
        shutdowns += 1

    lifespan_app = Starlette(lifespan=lifespan)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifespan_app(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        raise RuntimeError("late exception")

    client = test_client_factory(app)
    with client:
        with pytest.raises(RuntimeError, match="late exception"):
            client.get("/")
    assert shutdowns == 1
