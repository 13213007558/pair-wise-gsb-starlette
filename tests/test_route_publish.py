from __future__ import annotations

import threading

import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import (
    Host,
    Mount,
    PublishRecord,
    RollbackRecord,
    Route,
    Router,
    RouteTableError,
    WebSocketRoute,
)
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect


def text_endpoint(text: str):
    def endpoint(request: Request) -> PlainTextResponse:
        return PlainTextResponse(text)

    return endpoint


def test_publish_replaces_route_table_without_restart() -> None:
    app = Starlette(routes=[Route("/old", endpoint=text_endpoint("old"), name="old")])
    client = TestClient(app)
    assert client.get("/old").text == "old"
    old_version = app.route_table.version

    table = app.publish_routes([Route("/new", endpoint=text_endpoint("new"), name="new")])

    assert table.version == old_version + 1
    assert app.route_table is table
    assert app.router.route_table_version == table.version
    assert app.router.publish_history == [PublishRecord(version=table.version, route_count=1)]
    assert client.get("/old").status_code == 404
    assert client.get("/new").text == "new"


def test_published_table_isolated_from_caller_list_mutation() -> None:
    routes = [Route("/a", endpoint=text_endpoint("a"), name="a")]
    app = Starlette(routes=[])
    app.publish_routes(routes)

    # Later mutations of the caller's list must not change the published version.
    routes.append(Route("/sneaky", endpoint=text_endpoint("sneaky"), name="sneaky"))
    routes.clear()

    client = TestClient(app)
    assert client.get("/a").text == "a"
    assert client.get("/sneaky").status_code == 404


def test_published_table_isolated_from_mounted_router_mutation() -> None:
    inner = Router(routes=[Route("/a", endpoint=text_endpoint("a"), name="a")])
    app = Starlette(routes=[Mount("/sub", app=inner)])
    client = TestClient(app)
    assert client.get("/sub/a").text == "a"

    # Mutating the mounted router must not leak into the published version.
    inner.add_route("/b", endpoint=text_endpoint("b"), name="b")
    assert client.get("/sub/b").status_code == 404

    # ...until the control plane explicitly republishes.
    app.publish_routes([Mount("/sub", app=inner)])
    assert client.get("/sub/b").text == "b"


def test_in_flight_request_keeps_entering_version() -> None:
    entered = threading.Event()
    release = threading.Event()

    def slow(request: Request) -> JSONResponse:
        entered.set()
        assert release.wait(5)
        return JSONResponse({"url": str(request.url_for("item")), "handler": "v1"})

    app = Starlette(
        routes=[
            Route("/slow", endpoint=slow, name="slow"),
            Route("/item", endpoint=text_endpoint("item"), name="item"),
        ]
    )
    client = TestClient(app)

    holder: dict[str, object] = {}

    def do_request() -> None:
        holder["response"] = client.get("/slow")

    thread = threading.Thread(target=do_request)
    thread.start()
    assert entered.wait(5)

    app.publish_routes([Route("/thing", endpoint=text_endpoint("thing"), name="item")])

    release.set()
    thread.join(5)
    response = holder["response"]
    # The request that entered before the publish keeps version 1 for both
    # the matched handler and request.url_for().
    assert response.status_code == 200
    assert response.json() == {"url": "http://testserver/item", "handler": "v1"}

    # Requests entering after the publish use the new version.
    assert client.get("/slow").status_code == 404
    assert client.get("/thing").text == "thing"


def test_redirect_slashes_follows_published_version() -> None:
    app = Starlette(routes=[Route("/users/", endpoint=text_endpoint("users"), name="users")])
    client = TestClient(app)

    response = client.get("/users", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/users/"

    app.publish_routes([Route("/users", endpoint=text_endpoint("users"), name="users")])

    response = client.get("/users", follow_redirects=False)
    assert response.status_code == 200
    response = client.get("/users/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/users"


def test_url_for_uses_pinned_version_across_mounts_and_hosts() -> None:
    entered = threading.Event()
    release = threading.Event()

    def slow(request: Request) -> JSONResponse:
        entered.set()
        assert release.wait(5)
        return JSONResponse(
            {
                "mount": str(request.url_for("sub:page")),
                "host": str(request.url_for("api:page")),
            }
        )

    def build(mount_path: str, host: str) -> list:
        return [
            Route("/slow", endpoint=slow, name="slow"),
            Mount(mount_path, name="sub", routes=[Route("/page", endpoint=text_endpoint("page"), name="page")]),
            Host(host, name="api", app=Router(routes=[Route("/page", endpoint=text_endpoint("page"), name="page")])),
        ]

    app = Starlette(routes=build("/sub", "api.example.org"))
    client = TestClient(app)

    holder: dict[str, object] = {}

    def do_request() -> None:
        holder["response"] = client.get("/slow")

    thread = threading.Thread(target=do_request)
    thread.start()
    assert entered.wait(5)

    app.publish_routes(build("/sub2", "api2.example.org"))

    release.set()
    thread.join(5)
    response = holder["response"]
    # url_for() inside the in-flight request resolves against the entering
    # version, through nested Mount and Host namespaces.
    assert response.json() == {
        "mount": "http://testserver/sub/page",
        "host": "http://api.example.org/page",
    }

    # New requests resolve against the new version.
    assert client.get("/sub2/page").text == "page"
    assert client.get("/sub/page").status_code == 404
    assert client.get("/page", headers={"host": "api2.example.org"}).text == "page"
    assert client.get("/page", headers={"host": "api.example.org"}).status_code == 404


def test_url_path_for_outside_request_uses_current_version() -> None:
    app = Starlette(routes=[Route("/a", endpoint=text_endpoint("a"), name="page")])
    assert app.url_path_for("page") == "/a"
    app.publish_routes([Route("/b", endpoint=text_endpoint("b"), name="page")])
    assert app.url_path_for("page") == "/b"


def test_publish_rejects_invalid_convertor() -> None:
    app = Starlette(routes=[Route("/ok", endpoint=text_endpoint("ok"), name="ok")])
    version_before = app.route_table.version

    bad = Route("/bad", endpoint=text_endpoint("bad"), name="bad")
    bad.path = "/{param:bogus}"  # a route carrying an unknown convertor

    with pytest.raises(RouteTableError) as exc_info:
        app.publish_routes([bad])
    assert "bogus" in str(exc_info.value)

    # The failed publish keeps the old table and its version number, and the
    # rollback evidence is recorded separately.
    assert app.route_table.version == version_before
    assert app.router.publish_history == []
    assert app.router.rollback_log == [RollbackRecord(version=version_before, errors=tuple(exc_info.value.errors))]

    client = TestClient(app)
    assert client.get("/ok").text == "ok"
    assert client.get("/bad").status_code == 404


def test_publish_rejects_circular_mount() -> None:
    app = Starlette(routes=[Route("/ok", endpoint=text_endpoint("ok"), name="ok")])
    version_before = app.route_table.version

    with pytest.raises(RouteTableError, match="Circular mount"):
        app.publish_routes([Mount("/loop", app=app.router)])

    # Indirect cycles are detected as well.
    with pytest.raises(RouteTableError, match="Circular mount"):
        app.publish_routes([Mount("/outer", routes=[Mount("/inner", app=app.router)])])

    assert app.route_table.version == version_before
    assert len(app.router.rollback_log) == 2
    assert all(record.version == version_before for record in app.router.rollback_log)

    client = TestClient(app)
    assert client.get("/ok").text == "ok"


def test_publish_rejects_duplicate_route_names() -> None:
    app = Starlette(routes=[])

    with pytest.raises(RouteTableError, match="Duplicate route name 'dup'"):
        app.publish_routes(
            [
                Route("/a", endpoint=text_endpoint("a"), name="dup"),
                Route("/b", endpoint=text_endpoint("b"), name="dup"),
            ]
        )

    # Duplicate mount names at the same level are namespace conflicts.
    with pytest.raises(RouteTableError, match="Duplicate route name 'sub'"):
        app.publish_routes(
            [
                Mount("/a", name="sub", routes=[Route("/x", endpoint=text_endpoint("x"), name="x")]),
                Mount("/b", name="sub", routes=[Route("/y", endpoint=text_endpoint("y"), name="y")]),
            ]
        )

    assert app.router.publish_history == []
    assert len(app.router.rollback_log) == 2

    # The same name in different mount namespaces is acceptable.
    table = app.publish_routes(
        [
            Mount("/a", name="a", routes=[Route("/page", endpoint=text_endpoint("pa"), name="page")]),
            Mount("/b", name="b", routes=[Route("/page", endpoint=text_endpoint("pb"), name="page")]),
        ]
    )
    assert app.route_table is table
    client = TestClient(app)
    assert client.get("/a/page").text == "pa"
    assert client.get("/b/page").text == "pb"


def test_failed_publish_preserves_old_table_and_version() -> None:
    app = Starlette(routes=[Route("/ok", endpoint=text_endpoint("ok"), name="ok")])
    old_table = app.route_table

    with pytest.raises(RouteTableError):
        app.publish_routes(
            [
                Route("/x", endpoint=text_endpoint("x"), name="dup"),
                Route("/y", endpoint=text_endpoint("y"), name="dup"),
            ]
        )

    assert app.route_table is old_table
    assert app.route_table.version == old_table.version
    assert app.router.publish_history == []
    assert len(app.router.rollback_log) == 1
    assert app.router.rollback_log[0].version == old_table.version

    client = TestClient(app)
    assert client.get("/ok").text == "ok"
    assert client.get("/x").status_code == 404


def test_publish_rejects_non_route_entries() -> None:
    app = Starlette(routes=[])
    with pytest.raises(RouteTableError, match="BaseRoute"):
        app.publish_routes([object()])  # type: ignore[list-item]
    assert len(app.router.rollback_log) == 1


def test_concurrent_publishes_are_serialized_and_traced() -> None:
    app = Starlette(routes=[])
    barrier = threading.Barrier(5)

    def publish(index: int) -> None:
        barrier.wait()
        app.publish_routes([Route(f"/r{index}", endpoint=text_endpoint(f"r{index}"), name=f"r{index}")])

    threads = [threading.Thread(target=publish, args=(index,)) for index in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Publishes are serialized: versions are assigned in acquisition order.
    history = app.router.publish_history
    assert [record.version for record in history] == [2, 3, 4, 5, 6]
    assert app.route_table.version == 6
    assert app.router.rollback_log == []

    # Exactly the last published table is in effect.
    client = TestClient(app)
    served = [index for index in range(5) if client.get(f"/r{index}").status_code == 200]
    assert len(served) == 1


def test_concurrent_requests_across_publish_use_a_single_version() -> None:
    app = Starlette(routes=[Route("/item", endpoint=text_endpoint("v1"), name="item")])
    client = TestClient(app)
    barrier = threading.Barrier(4)
    trace: list[str] = []
    trace_lock = threading.Lock()

    def get_item() -> None:
        barrier.wait()
        response = client.get("/item")
        with trace_lock:
            trace.append(response.text)

    threads = [threading.Thread(target=get_item) for _ in range(3)]
    for thread in threads:
        thread.start()

    # Release the workers and race the publish against them.
    barrier.wait()
    app.publish_routes([Route("/item", endpoint=text_endpoint("v2"), name="item")])
    for thread in threads:
        thread.join()

    # Every request observed exactly one consistent version.
    assert len(trace) == 3
    assert set(trace) <= {"v1", "v2"}
    # Requests entering after the publish completed see the new version.
    assert client.get("/item").text == "v2"


def test_url_404_405_results_traced_through_publish() -> None:
    app = Starlette(
        routes=[
            Route("/item", endpoint=text_endpoint("item-v1"), methods=["GET"], name="item"),
            Route("/legacy", endpoint=text_endpoint("legacy-v1"), name="legacy"),
        ]
    )
    client = TestClient(app)
    trace: list[tuple[str, object]] = []

    trace.append(("url", client.get("/item").text))
    trace.append(("404", client.get("/missing").status_code))
    trace.append(("405", client.post("/item").status_code))

    app.publish_routes(
        [
            Route("/item", endpoint=text_endpoint("item-v2"), methods=["POST"], name="item"),
            Route("/fresh", endpoint=text_endpoint("fresh-v2"), name="fresh"),
        ]
    )

    trace.append(("url", client.get("/item").status_code))
    trace.append(("url", client.post("/item").text))
    trace.append(("404", client.get("/legacy").status_code))
    trace.append(("405", client.get("/item").status_code))
    trace.append(("url", client.get("/fresh").text))

    assert trace == [
        ("url", "item-v1"),
        ("404", 404),
        ("405", 405),
        ("url", 405),  # GET is no longer allowed on /item in version 2
        ("url", "item-v2"),
        ("404", 404),  # /legacy was removed in version 2
        ("405", 405),
        ("url", "fresh-v2"),
    ]


def test_websocket_connections_keep_entering_version() -> None:
    async def ws_v1(websocket: WebSocket) -> None:
        await websocket.accept()
        message = await websocket.receive_text()
        await websocket.send_text("v1:" + message)
        await websocket.close()

    async def ws_v2(websocket: WebSocket) -> None:
        await websocket.accept()
        message = await websocket.receive_text()
        await websocket.send_text("v2:" + message)
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", endpoint=ws_v1, name="ws")])
    client = TestClient(app)

    # A connection that entered before the publish keeps its version.
    with client.websocket_connect("/ws") as session:
        app.publish_routes([WebSocketRoute("/ws", endpoint=ws_v2, name="ws")])
        session.send_text("hello")
        assert session.receive_text() == "v1:hello"

    # New connections use the new version.
    with client.websocket_connect("/ws") as session:
        session.send_text("hello")
        assert session.receive_text() == "v2:hello"


def test_websocket_404_traced_through_publish() -> None:
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.close()

    app = Starlette(routes=[])
    client = TestClient(app)
    trace: list[tuple[str, object]] = []

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass  # pragma: no cover
    trace.append(("ws-404", "closed"))

    app.publish_routes([WebSocketRoute("/ws", endpoint=ws, name="ws")])
    with client.websocket_connect("/ws"):
        trace.append(("ws", "accepted"))

    assert trace == [("ws-404", "closed"), ("ws", "accepted")]


def test_middleware_wrapped_mount_uses_pinned_version() -> None:
    class PassthroughMiddleware:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await self.app(scope, receive, send)

    inner = Router(routes=[Route("/a", endpoint=text_endpoint("a"), name="a")])
    mount = Mount("/sub", app=inner, name="sub", middleware=[Middleware(PassthroughMiddleware)])
    app = Starlette(routes=[mount])
    client = TestClient(app)

    assert client.get("/sub/a").text == "a"
    assert app.url_path_for("sub:a") == "/sub/a"

    # Mutating the mounted router must not leak into the published version,
    # even when the mount is wrapped in middleware.
    inner.add_route("/b", endpoint=text_endpoint("b"), name="b")
    assert client.get("/sub/b").status_code == 404


def test_router_helpers_publish_new_versions() -> None:
    router = Router(routes=[Route("/a", endpoint=text_endpoint("a"), name="a")])
    version = router.route_table_version

    router.add_route("/b", endpoint=text_endpoint("b"), name="b")
    assert router.route_table_version == version + 1

    client = TestClient(router)
    assert client.get("/a").text == "a"
    assert client.get("/b").text == "b"
