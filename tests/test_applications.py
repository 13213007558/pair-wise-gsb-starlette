from __future__ import annotations

import os
from collections.abc import AsyncGenerator, AsyncIterator, Generator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

import anyio.from_thread
import pytest

from starlette import status
from starlette.applications import Starlette
from starlette.endpoints import HTTPEndpoint
from starlette.exceptions import HTTPException, WebSocketException
from starlette.middleware import Middleware
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Host, Mount, Route, Router, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket
from tests.types import TestClientFactory


async def error_500(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": "Server Error"}, status_code=500)


async def method_not_allowed(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": "Custom message"}, status_code=405)


async def http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


def func_homepage(request: Request) -> PlainTextResponse:
    return PlainTextResponse("Hello, world!")


async def async_homepage(request: Request) -> PlainTextResponse:
    return PlainTextResponse("Hello, world!")


class Homepage(HTTPEndpoint):
    def get(self, request: Request) -> PlainTextResponse:
        return PlainTextResponse("Hello, world!")


def all_users_page(request: Request) -> PlainTextResponse:
    return PlainTextResponse("Hello, everyone!")


def user_page(request: Request) -> PlainTextResponse:
    username = request.path_params["username"]
    return PlainTextResponse(f"Hello, {username}!")


def custom_subdomain(request: Request) -> PlainTextResponse:
    return PlainTextResponse("Subdomain: " + request.path_params["subdomain"])


def runtime_error(request: Request) -> None:
    raise RuntimeError()


async def websocket_endpoint(session: WebSocket) -> None:
    await session.accept()
    await session.send_text("Hello, world!")
    await session.close()


async def websocket_raise_websocket_exception(websocket: WebSocket) -> None:
    await websocket.accept()
    raise WebSocketException(code=status.WS_1003_UNSUPPORTED_DATA)


async def websocket_raise_http_exception(websocket: WebSocket) -> None:
    raise HTTPException(status_code=401, detail="Unauthorized")


class CustomWSException(Exception):
    pass


async def websocket_raise_custom(websocket: WebSocket) -> None:
    await websocket.accept()
    raise CustomWSException()


def custom_ws_exception_handler(websocket: WebSocket, exc: CustomWSException) -> None:
    anyio.from_thread.run(websocket.close, status.WS_1013_TRY_AGAIN_LATER)


users = Router(
    routes=[
        Route("/", endpoint=all_users_page),
        Route("/{username}", endpoint=user_page),
    ]
)

subdomain = Router(
    routes=[
        Route("/", custom_subdomain),
    ]
)

exception_handlers = {
    500: error_500,
    405: method_not_allowed,
    HTTPException: http_exception,
    CustomWSException: custom_ws_exception_handler,
}

middleware = [Middleware(TrustedHostMiddleware, allowed_hosts=["testserver", "*.example.org"])]

app = Starlette(
    routes=[
        Route("/func", endpoint=func_homepage),
        Route("/async", endpoint=async_homepage),
        Route("/class", endpoint=Homepage),
        Route("/500", endpoint=runtime_error),
        WebSocketRoute("/ws", endpoint=websocket_endpoint),
        WebSocketRoute("/ws-raise-websocket", endpoint=websocket_raise_websocket_exception),
        WebSocketRoute("/ws-raise-http", endpoint=websocket_raise_http_exception),
        WebSocketRoute("/ws-raise-custom", endpoint=websocket_raise_custom),
        Mount("/users", app=users),
        Host("{subdomain}.example.org", app=subdomain),
    ],
    exception_handlers=exception_handlers,  # type: ignore
    middleware=middleware,
)


@pytest.fixture
def client(test_client_factory: TestClientFactory) -> Generator[TestClient, None, None]:
    with test_client_factory(app) as client:
        yield client


def test_url_path_for() -> None:
    assert app.url_path_for("func_homepage") == "/func"


def test_func_route(client: TestClient) -> None:
    response = client.get("/func")
    assert response.status_code == 200
    assert response.text == "Hello, world!"

    response = client.head("/func")
    assert response.status_code == 200
    assert response.text == ""


def test_async_route(client: TestClient) -> None:
    response = client.get("/async")
    assert response.status_code == 200
    assert response.text == "Hello, world!"


def test_class_route(client: TestClient) -> None:
    response = client.get("/class")
    assert response.status_code == 200
    assert response.text == "Hello, world!"


def test_mounted_route(client: TestClient) -> None:
    response = client.get("/users/")
    assert response.status_code == 200
    assert response.text == "Hello, everyone!"


def test_mounted_route_path_params(client: TestClient) -> None:
    response = client.get("/users/tomchristie")
    assert response.status_code == 200
    assert response.text == "Hello, tomchristie!"


def test_subdomain_route(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app, base_url="https://foo.example.org/")

    response = client.get("/")
    assert response.status_code == 200
    assert response.text == "Subdomain: foo"


def test_websocket_route(client: TestClient) -> None:
    with client.websocket_connect("/ws") as session:
        text = session.receive_text()
        assert text == "Hello, world!"


def test_400(client: TestClient) -> None:
    response = client.get("/404")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_405(client: TestClient) -> None:
    response = client.post("/func")
    assert response.status_code == 405
    assert response.json() == {"detail": "Custom message"}

    response = client.post("/class")
    assert response.status_code == 405
    assert response.json() == {"detail": "Custom message"}


def test_500(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app, raise_server_exceptions=False)
    response = client.get("/500")
    assert response.status_code == 500
    assert response.json() == {"detail": "Server Error"}


def test_websocket_raise_websocket_exception(client: TestClient) -> None:
    with client.websocket_connect("/ws-raise-websocket") as session:
        response = session.receive()
        assert response == {
            "type": "websocket.close",
            "code": status.WS_1003_UNSUPPORTED_DATA,
            "reason": "",
        }


def test_websocket_raise_http_exception(client: TestClient) -> None:
    with pytest.raises(WebSocketDenialResponse) as exc:
        with client.websocket_connect("/ws-raise-http"):
            pass  # pragma: no cover
    assert exc.value.status_code == 401
    assert exc.value.content == b'{"detail":"Unauthorized"}'


def test_websocket_raise_custom_exception(client: TestClient) -> None:
    with client.websocket_connect("/ws-raise-custom") as session:
        response = session.receive()
        assert response == {
            "type": "websocket.close",
            "code": status.WS_1013_TRY_AGAIN_LATER,
            "reason": "",
        }


def test_middleware(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app, base_url="http://incorrecthost")
    response = client.get("/func")
    assert response.status_code == 400
    assert response.text == "Invalid host header"


def test_routes() -> None:
    assert app.routes == [
        Route("/func", endpoint=func_homepage, methods=["GET"]),
        Route("/async", endpoint=async_homepage, methods=["GET"]),
        Route("/class", endpoint=Homepage),
        Route("/500", endpoint=runtime_error, methods=["GET"]),
        WebSocketRoute("/ws", endpoint=websocket_endpoint),
        WebSocketRoute("/ws-raise-websocket", endpoint=websocket_raise_websocket_exception),
        WebSocketRoute("/ws-raise-http", endpoint=websocket_raise_http_exception),
        WebSocketRoute("/ws-raise-custom", endpoint=websocket_raise_custom),
        Mount(
            "/users",
            app=Router(
                routes=[
                    Route("/", endpoint=all_users_page),
                    Route("/{username}", endpoint=user_page),
                ]
            ),
        ),
        Host(
            "{subdomain}.example.org",
            app=Router(routes=[Route("/", endpoint=custom_subdomain)]),
        ),
    ]


def test_app_mount(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    app = Starlette(
        routes=[
            Mount("/static", StaticFiles(directory=tmpdir)),
        ]
    )

    client = test_client_factory(app)

    response = client.get("/static/example.txt")
    assert response.status_code == 200
    assert response.text == "<file content>"

    response = client.post("/static/example.txt")
    assert response.status_code == 405
    assert response.text == "Method Not Allowed"


def test_app_debug(test_client_factory: TestClientFactory) -> None:
    async def homepage(request: Request) -> None:
        raise RuntimeError()

    app = Starlette(
        routes=[
            Route("/", homepage),
        ],
    )
    app.debug = True

    client = test_client_factory(app, raise_server_exceptions=False)
    response = client.get("/")
    assert response.status_code == 500
    assert "RuntimeError" in response.text
    assert app.debug


def test_app_add_route(test_client_factory: TestClientFactory) -> None:
    async def homepage(request: Request) -> PlainTextResponse:
        return PlainTextResponse("Hello, World!")

    app = Starlette(
        routes=[
            Route("/", endpoint=homepage),
        ]
    )

    client = test_client_factory(app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.text == "Hello, World!"


def test_app_add_websocket_route(test_client_factory: TestClientFactory) -> None:
    async def websocket_endpoint(session: WebSocket) -> None:
        await session.accept()
        await session.send_text("Hello, world!")
        await session.close()

    app = Starlette(
        routes=[
            WebSocketRoute("/ws", endpoint=websocket_endpoint),
        ]
    )
    client = test_client_factory(app)

    with client.websocket_connect("/ws") as session:
        text = session.receive_text()
        assert text == "Hello, world!"


def test_app_add_event_handler(test_client_factory: TestClientFactory) -> None:
    startup_complete = False
    cleanup_complete = False

    def run_startup() -> None:
        nonlocal startup_complete
        startup_complete = True

    def run_cleanup() -> None:
        nonlocal cleanup_complete
        cleanup_complete = True

    with pytest.deprecated_call(match="The on_startup and on_shutdown parameters are deprecated"):
        app = Starlette(
            on_startup=[run_startup],
            on_shutdown=[run_cleanup],
        )

    assert not startup_complete
    assert not cleanup_complete
    with test_client_factory(app):
        assert startup_complete
        assert not cleanup_complete
    assert startup_complete
    assert cleanup_complete


def test_app_async_cm_lifespan(test_client_factory: TestClientFactory) -> None:
    startup_complete = False
    cleanup_complete = False

    @asynccontextmanager
    async def lifespan(app: ASGIApp) -> AsyncGenerator[None, None]:
        nonlocal startup_complete, cleanup_complete
        startup_complete = True
        yield
        cleanup_complete = True

    app = Starlette(lifespan=lifespan)

    assert not startup_complete
    assert not cleanup_complete
    with test_client_factory(app):
        assert startup_complete
        assert not cleanup_complete
    assert startup_complete
    assert cleanup_complete


deprecated_lifespan = pytest.mark.filterwarnings(
    r"ignore"
    r":(async )?generator function lifespans are deprecated, use an "
    r"@contextlib\.asynccontextmanager function instead"
    r":DeprecationWarning"
    r":starlette.routing"
)


@deprecated_lifespan
def test_app_async_gen_lifespan(test_client_factory: TestClientFactory) -> None:
    startup_complete = False
    cleanup_complete = False

    async def lifespan(app: ASGIApp) -> AsyncGenerator[None, None]:
        nonlocal startup_complete, cleanup_complete
        startup_complete = True
        yield
        cleanup_complete = True

    app = Starlette(lifespan=lifespan)  # type: ignore

    assert not startup_complete
    assert not cleanup_complete
    with test_client_factory(app):
        assert startup_complete
        assert not cleanup_complete
    assert startup_complete
    assert cleanup_complete


@deprecated_lifespan
def test_app_sync_gen_lifespan(test_client_factory: TestClientFactory) -> None:
    startup_complete = False
    cleanup_complete = False

    def lifespan(app: ASGIApp) -> Generator[None, None, None]:
        nonlocal startup_complete, cleanup_complete
        startup_complete = True
        yield
        cleanup_complete = True

    app = Starlette(lifespan=lifespan)  # type: ignore

    assert not startup_complete
    assert not cleanup_complete
    with test_client_factory(app):
        assert startup_complete
        assert not cleanup_complete
    assert startup_complete
    assert cleanup_complete


def test_decorator_deprecations() -> None:
    app = Starlette()

    with pytest.deprecated_call(
        match=("The `exception_handler` decorator is deprecated, and will be removed in version 1.0.0.")
    ) as record:
        app.exception_handler(500)(http_exception)
        assert len(record) == 1

    with pytest.deprecated_call(
        match=("The `middleware` decorator is deprecated, and will be removed in version 1.0.0.")
    ) as record:

        async def middleware(request: Request, call_next: RequestResponseEndpoint) -> None: ...  # pragma: no cover

        app.middleware("http")(middleware)
        assert len(record) == 1

    with pytest.deprecated_call(
        match=("The `route` decorator is deprecated, and will be removed in version 1.0.0.")
    ) as record:
        app.route("/")(async_homepage)
        assert len(record) == 1

    with pytest.deprecated_call(
        match=("The `websocket_route` decorator is deprecated, and will be removed in version 1.0.0.")
    ) as record:
        app.websocket_route("/ws")(websocket_endpoint)
        assert len(record) == 1

    with pytest.deprecated_call(
        match=("The `on_event` decorator is deprecated, and will be removed in version 1.0.0.")
    ) as record:

        async def startup() -> None: ...  # pragma: no cover

        app.on_event("startup")(startup)
        assert len(record) == 1


def test_middleware_stack_init(test_client_factory: TestClientFactory) -> None:
    class NoOpMiddleware:
        def __init__(self, app: ASGIApp):
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await self.app(scope, receive, send)

    class SimpleInitializableMiddleware:
        counter = 0

        def __init__(self, app: ASGIApp):
            self.app = app
            SimpleInitializableMiddleware.counter += 1

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await self.app(scope, receive, send)

    def get_app() -> ASGIApp:
        app = Starlette()
        app.add_middleware(SimpleInitializableMiddleware)
        app.add_middleware(NoOpMiddleware)
        return app

    app = get_app()

    with test_client_factory(app):
        pass

    assert SimpleInitializableMiddleware.counter == 1

    test_client_factory(app).get("/foo")

    assert SimpleInitializableMiddleware.counter == 1

    app = get_app()

    test_client_factory(app).get("/foo")

    assert SimpleInitializableMiddleware.counter == 2


def test_middleware_args(test_client_factory: TestClientFactory) -> None:
    calls: list[str] = []

    class MiddlewareWithArgs:
        def __init__(self, app: ASGIApp, arg: str) -> None:
            self.app = app
            self.arg = arg

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            calls.append(self.arg)
            await self.app(scope, receive, send)

    app = Starlette()
    app.add_middleware(MiddlewareWithArgs, "foo")
    app.add_middleware(MiddlewareWithArgs, "bar")

    with test_client_factory(app):
        pass

    assert calls == ["bar", "foo"]


def test_middleware_factory(test_client_factory: TestClientFactory) -> None:
    calls: list[str] = []

    def _middleware_factory(app: ASGIApp, arg: str) -> ASGIApp:
        async def _app(scope: Scope, receive: Receive, send: Send) -> None:
            calls.append(arg)
            await app(scope, receive, send)

        return _app

    def get_middleware_factory() -> Callable[[ASGIApp, str], ASGIApp]:
        return _middleware_factory

    app = Starlette()
    app.add_middleware(_middleware_factory, arg="foo")
    app.add_middleware(get_middleware_factory(), "bar")

    with test_client_factory(app):
        pass

    assert calls == ["bar", "foo"]


def test_lifespan_app_subclass() -> None:
    # This test exists to make sure that subclasses of Starlette
    # (like FastAPI) are compatible with the types hints for Lifespan

    class App(Starlette):
        pass

    @asynccontextmanager
    async def lifespan(app: App) -> AsyncIterator[None]:  # pragma: no cover
        yield

    App(lifespan=lifespan)


def test_composed_mounted_lifespans_startup_order(
    test_client_factory: TestClientFactory,
) -> None:
    events: list[str] = []

    def lifespan(name: str) -> Callable[[ASGIApp], AsyncIterator[dict[str, str]]]:
        @asynccontextmanager
        async def managed_app(app: ASGIApp) -> AsyncIterator[dict[str, str]]:
            events.append(f"start:{name}")
            try:
                yield {}
            finally:
                events.append(f"stop:{name}")

        return managed_app

    shared = Starlette(lifespan=lifespan("shared"))
    other = Starlette(lifespan=lifespan("other"))
    parent = Starlette(
        lifespan=lifespan("parent"),
        routes=[
            Mount("/shared", app=shared),
            Mount("/shared-again", app=shared),
        ],
    )
    nested = Router(routes=[Mount("/other", app=other)])
    root = Starlette(
        compose_mounted_lifespans=True,
        lifespan=lifespan("root"),
        routes=[
            Mount("/parent", app=parent),
            Mount("/nested", app=nested),
        ],
    )

    with test_client_factory(root):
        assert events == ["start:root", "start:parent", "start:shared", "start:other"]

    assert events == [
        "start:root",
        "start:parent",
        "start:shared",
        "start:other",
        "stop:other",
        "stop:shared",
        "stop:parent",
        "stop:root",
    ]


def test_mounted_lifespans_are_not_composed_by_default(
    test_client_factory: TestClientFactory,
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def child_lifespan(app: ASGIApp) -> AsyncIterator[None]:
        events.append("start")
        try:
            yield
        finally:
            events.append("stop")

    child = Starlette(lifespan=child_lifespan)
    root = Starlette(routes=[Mount("/child", app=child)])

    with test_client_factory(root):
        assert events == []
    assert events == []


def test_composed_mounted_lifespans_support_async_context_managers(
    test_client_factory: TestClientFactory,
) -> None:
    events: list[str] = []

    class Lifespan:
        def __init__(self, name: str) -> None:
            self.name = name

        async def __aenter__(self) -> None:
            events.append("start:" + self.name)

        async def __aexit__(self, *args: object) -> None:
            events.append("stop:" + self.name)

        def __call__(self, app: ASGIApp) -> Lifespan:
            return self

    child = Starlette(lifespan=Lifespan("child"))
    root = Starlette(
        compose_mounted_lifespans=True,
        lifespan=Lifespan("root"),
        routes=[Mount("/child", app=child)],
    )

    with test_client_factory(root):
        assert events == ["start:root", "start:child"]
    assert events == ["start:root", "start:child", "stop:child", "stop:root"]


def test_composed_mounted_lifespans_unwrap_nested_composition(
    test_client_factory: TestClientFactory,
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(name: str, app: ASGIApp) -> AsyncIterator[None]:
        events.append("start:" + name)
        try:
            yield
        finally:
            events.append("stop:" + name)

    grandchild = Starlette(lifespan=lambda app: lifespan("grandchild", app))
    child = Starlette(
        compose_mounted_lifespans=True,
        lifespan=lambda app: lifespan("child", app),
        routes=[Mount("/grandchild", app=grandchild)],
    )
    root = Starlette(
        compose_mounted_lifespans=True,
        lifespan=lambda app: lifespan("root", app),
        routes=[Mount("/child", app=child)],
    )

    with test_client_factory(root):
        assert events == ["start:root", "start:child", "start:grandchild"]
    assert events == [
        "start:root",
        "start:child",
        "start:grandchild",
        "stop:grandchild",
        "stop:child",
        "stop:root",
    ]


def test_composed_mounted_lifespan_state_isolation(
    test_client_factory: TestClientFactory,
) -> None:
    events: list[str] = []

    def lifespan(
        name: str,
        state: dict[str, str],
    ) -> Callable[[ASGIApp], AsyncIterator[dict[str, str]]]:
        @asynccontextmanager
        async def managed_app(app: ASGIApp) -> AsyncIterator[dict[str, str]]:
            events.append(f"start:{name}")
            try:
                yield state
            finally:
                events.append(f"stop:{name}")

        return managed_app

    async def show_state(request: Request) -> JSONResponse:
        return JSONResponse(dict(request.scope["state"]))

    async def mutate_state(request: Request) -> JSONResponse:
        request.state.shared = "mutated"
        request.state.child_only = "mutated"
        request.state.request_only = "mutated"
        return JSONResponse(dict(request.scope["state"]))

    shared_state = {
        "name": "shared",
        "shared": "child",
        "child_only": "shared",
    }
    other_state = {
        "name": "other",
        "shared": "other",
        "child_only": "other",
    }
    shared = Starlette(
        lifespan=lifespan("shared", shared_state),
        routes=[Route("/", endpoint=show_state)],
    )
    other = Starlette(
        lifespan=lifespan("other", other_state),
        routes=[
            Route("/", endpoint=show_state),
            Route("/mutate", endpoint=mutate_state),
        ],
    )
    grandchild = Starlette(
        lifespan=lifespan("grandchild", {"name": "grandchild", "grandchild_only": "grandchild"}),
        routes=[Route("/", endpoint=show_state)],
    )
    parent = Starlette(
        lifespan=lifespan("parent", {"parent_only": "parent"}),
        routes=[
            Mount("/grand", app=grandchild),
            Mount("/one", app=shared),
            Mount("/two", app=shared),
        ],
    )
    root = Starlette(
        compose_mounted_lifespans=True,
        lifespan=lifespan(
            "root",
            {"shared": "root", "root_only": "root"},
        ),
        routes=[
            Route("/", endpoint=show_state),
            Mount("/parent", app=parent),
            Mount("/other", app=other),
        ],
    )

    with test_client_factory(root) as client:
        assert client.get("/").json() == {
            "shared": "root",
            "root_only": "root",
        }

        first = client.get("/parent/one/")
        expected_shared_state = {
            "shared": "child",
            "root_only": "root",
            "parent_only": "parent",
            "child_only": "shared",
            "name": "shared",
        }
        assert first.json() == expected_shared_state
        assert client.get("/parent/two/").json() == expected_shared_state

        assert client.get("/parent/grand/").json() == {
            "shared": "root",
            "root_only": "root",
            "parent_only": "parent",
            "grandchild_only": "grandchild",
            "name": "grandchild",
        }

        assert client.get("/other/").json() == {
            "shared": "other",
            "root_only": "root",
            "child_only": "other",
            "name": "other",
        }

        assert client.get("/other/mutate").json() == {
            "shared": "mutated",
            "root_only": "root",
            "child_only": "mutated",
            "name": "other",
            "request_only": "mutated",
        }
        assert client.get("/").json() == {
            "shared": "root",
            "root_only": "root",
        }
        assert client.get("/other/").json() == {
            "shared": "other",
            "root_only": "root",
            "child_only": "other",
            "name": "other",
        }

    assert events.count("start:shared") == 1
    assert events.count("stop:shared") == 1


def test_composed_mounted_lifespan_startup_failure_rollback(
    test_client_factory: TestClientFactory,
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def root_lifespan(app: ASGIApp) -> AsyncIterator[None]:
        events.append("start:root")
        try:
            yield
        finally:
            events.append("stop:root")

    @asynccontextmanager
    async def started_lifespan(app: ASGIApp) -> AsyncIterator[None]:
        events.append("start:started")
        try:
            yield
        finally:
            events.append("stop:started")

    @asynccontextmanager
    async def failing_lifespan(app: ASGIApp) -> AsyncIterator[None]:
        events.append("start:failing")
        raise RuntimeError("startup failed")
        yield  # pragma: no cover

    @asynccontextmanager
    async def later_lifespan(app: ASGIApp) -> AsyncIterator[None]:
        events.append("start:later")  # pragma: no cover
        yield  # pragma: no cover

    started = Starlette(lifespan=started_lifespan)
    failing = Starlette(lifespan=failing_lifespan)
    later = Starlette(lifespan=later_lifespan)
    root = Starlette(
        compose_mounted_lifespans=True,
        lifespan=root_lifespan,
        routes=[
            Mount("/started", app=started),
            Mount("/failing", app=failing),
            Mount("/later", app=later),
        ],
    )

    with pytest.raises(RuntimeError, match="startup failed"):
        with test_client_factory(root):
            pass  # pragma: no cover

    assert events == [
        "start:root",
        "start:started",
        "start:failing",
        "stop:started",
        "stop:root",
    ]


def test_composed_mounted_lifespan_reports_startup_failure(
    test_client_factory: TestClientFactory,
) -> None:
    @asynccontextmanager
    async def failing_lifespan(app: ASGIApp) -> AsyncIterator[None]:
        raise RuntimeError("startup failed")
        yield  # pragma: no cover

    child = Starlette(lifespan=failing_lifespan)
    root = Starlette(
        compose_mounted_lifespans=True,
        routes=[Mount("/child", app=child)],
    )
    messages: list[str] = []

    async def reporting_app(scope: Scope, receive: Receive, send: Send) -> None:
        async def report(message: dict[str, str]) -> None:
            messages.append(message["type"])
            await send(message)

        await root(scope, receive, report)

    with pytest.raises(RuntimeError, match="startup failed"):
        with test_client_factory(reporting_app):
            pass  # pragma: no cover

    assert messages == ["lifespan.startup.failed"]


def test_composed_mounted_lifespan_shutdown_failures_are_combined(
    test_client_factory: TestClientFactory,
) -> None:
    class ShutdownFailure(RuntimeError):
        def __init__(self, name: str) -> None:
            super().__init__(f"shutdown failed: {name}")
            self.name = name

    def lifespan(name: str) -> Callable[[ASGIApp], AsyncIterator[None]]:
        @asynccontextmanager
        async def managed_app(app: ASGIApp) -> AsyncIterator[None]:
            try:
                yield
            finally:
                raise ShutdownFailure(name)

        return managed_app

    second = Starlette(lifespan=lifespan("second"))
    first = Starlette(
        lifespan=lifespan("first"),
        routes=[Mount("/second", app=second)],
    )
    root = Starlette(
        compose_mounted_lifespans=True,
        lifespan=lifespan("root"),
        routes=[Mount("/first", app=first)],
    )

    with pytest.raises(ShutdownFailure) as exc_info:
        with test_client_factory(root):
            pass

    names = []
    exception: BaseException | None = exc_info.value
    while exception is not None:
        names.append(getattr(exception, "name", None))
        exception = exception.__context__

    assert names == ["root", "first", "second"]


def test_composed_mounted_lifespans_reject_cycles_before_startup(
    test_client_factory: TestClientFactory,
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def root_lifespan(app: ASGIApp) -> AsyncIterator[None]:
        events.append("start:root")  # pragma: no cover
        yield  # pragma: no cover

    child = Starlette()
    root = Starlette(
        compose_mounted_lifespans=True,
        lifespan=root_lifespan,
        routes=[Mount("/child", app=child)],
    )
    child.router.routes.append(Mount("/root", app=root))

    with pytest.raises(RuntimeError, match="cyclic mount graph"):
        with test_client_factory(root):
            pass  # pragma: no cover

    assert events == []
