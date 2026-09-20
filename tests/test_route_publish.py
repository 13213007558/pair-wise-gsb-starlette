from __future__ import annotations

import asyncio
import collections
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.convertors import CONVERTOR_TYPES, Convertor
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Host, Mount, Route, Router, RouteTableError, WebSocketRoute
from starlette.websockets import WebSocket

EVIDENCE_DIR = Path(os.environ.get("ROUTE_PUBLISH_EVIDENCE_DIR") or Path(__file__).resolve().parents[1] / "evidence")
TRACE_FILE = EVIDENCE_DIR / "route_publish_trace.jsonl"
ROLLBACK_FILE = EVIDENCE_DIR / "route_publish_rollback.json"


@pytest.fixture(autouse=True, scope="module")
def _trace_file() -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    TRACE_FILE.write_text("")


def record_trace(entry: dict[str, Any]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    with TRACE_FILE.open("a") as file:
        file.write(json.dumps(entry, sort_keys=True) + "\n")


def save_rollback_evidence(payload: dict[str, Any]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    ROLLBACK_FILE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


class AsyncBarrier:
    """A reusable asyncio barrier for a known number of parties."""

    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._count = 0
        self._event = asyncio.Event()

    async def wait(self) -> None:
        self._count += 1
        if self._count >= self._parties:
            self._event.set()
        await self._event.wait()


async def http_request(
    app: Starlette, path: str, method: str = "GET", host: str = "testserver"
) -> tuple[int, dict[str, str], bytes]:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "asgi": {"version": "3.0"},
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", host.encode())],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    headers = {key.decode(): value.decode() for key, value in start["headers"]}
    return start["status"], headers, body


async def ws_request(app: Starlette, path: str, host: str = "testserver") -> list[dict[str, Any]]:
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", host.encode())],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }
    messages: list[dict[str, Any]] = []
    inbox: collections.deque[dict[str, Any]] = collections.deque([{"type": "websocket.connect"}])

    async def receive() -> dict[str, Any]:
        if inbox:
            return inbox.popleft()
        return {"type": "websocket.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    return messages


def ws_texts(messages: list[dict[str, Any]]) -> list[str]:
    return [message["text"] for message in messages if message["type"] == "websocket.send"]


def ok_endpoint(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def ok_ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text("ok")
    await websocket.close()


def test_publish_swaps_table_for_new_requests() -> None:
    async def main() -> None:
        app = Starlette(routes=[Route("/users", ok_endpoint, name="users")])
        assert app.route_table.version == 0

        table = app.publish_routes([Route("/v2/users", ok_endpoint, name="users")])
        assert table.version == 1
        assert app.route_table is table

        status, _, _ = await http_request(app, "/v2/users")
        assert status == 200
        status, _, _ = await http_request(app, "/users")
        assert status == 404
        assert str(app.url_path_for("users")) == "/v2/users"

    asyncio.run(main())


def test_published_table_is_immutable_to_caller_mutations() -> None:
    async def main() -> None:
        app = Starlette(routes=[])
        candidate = [Route("/a", ok_endpoint, name="a")]
        table = app.publish_routes(candidate)

        # Mutating the list handed to publish_routes() afterwards must not
        # leak into the published version.
        candidate.append(Route("/sneaky", ok_endpoint, name="sneaky"))
        candidate.clear()

        status, _, _ = await http_request(app, "/a")
        assert status == 200
        status, _, _ = await http_request(app, "/sneaky")
        assert status == 404
        assert [route.path for route in table.routes] == ["/a"]

    asyncio.run(main())


def test_in_flight_request_keeps_entry_version() -> None:
    async def main() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        observed: dict[str, Any] = {}

        async def slow(request: Request) -> PlainTextResponse:
            observed["version_at_entry"] = request.scope["route_table"].version
            started.set()
            await release.wait()
            # The new table was published while this request was in flight,
            # but url_for() must still resolve against the pinned version.
            observed["url"] = str(request.url_for("users"))
            observed["version_in_scope"] = request.scope["route_table"].version
            return PlainTextResponse("slow")

        app = Starlette(routes=[Route("/slow", slow), Route("/users", ok_endpoint, name="users")])
        task = asyncio.create_task(http_request(app, "/slow"))
        await started.wait()

        app.publish_routes([Route("/v2/users", ok_endpoint, name="users")])
        release.set()
        status, _, _ = await task

        assert status == 200
        assert observed == {
            "version_at_entry": 0,
            "url": "http://testserver/users",
            "version_in_scope": 0,
        }
        # Requests entering after the publish use the new version.
        status, _, _ = await http_request(app, "/v2/users")
        assert status == 200
        status, _, _ = await http_request(app, "/users")
        assert status == 404

    asyncio.run(main())


def test_endpoint_publishing_mid_request_still_sees_pinned_table() -> None:
    async def main() -> None:
        v2_routes = [Route("/v2/users", ok_endpoint, name="users")]

        async def trigger(request: Request) -> JSONResponse:
            request.app.publish_routes(v2_routes)
            return JSONResponse(
                {
                    "url": str(request.url_for("users")),
                    "version": request.scope["route_table"].version,
                }
            )

        app = Starlette(routes=[Route("/trigger", trigger), Route("/users", ok_endpoint, name="users")])
        status, _, body = await http_request(app, "/trigger")
        assert status == 200
        assert json.loads(body) == {"url": "http://testserver/users", "version": 0}
        assert app.route_table.version == 1

    asyncio.run(main())


def test_trailing_slash_redirect_follows_published_version() -> None:
    async def main() -> None:
        app = Starlette(routes=[Route("/items/", ok_endpoint, name="items")])

        status, headers, _ = await http_request(app, "/items")
        assert status == 307
        assert headers["location"] == "http://testserver/items/"

        app.publish_routes([Route("/items", ok_endpoint, name="items")])
        status, _, _ = await http_request(app, "/items")
        assert status == 200
        status, headers, _ = await http_request(app, "/items/")
        assert status == 307
        assert headers["location"] == "http://testserver/items"

    asyncio.run(main())


def test_nested_mount_host_and_websocket_share_one_version() -> None:
    async def main() -> None:
        v2_routes = [
            Mount("/v2/api", routes=[Route("/users", ok_endpoint, name="users")], name="api"),
            Host(
                "api.example.com",
                app=Router(routes=[Route("/v2/home", ok_endpoint, name="home")]),
                name="hostns",
            ),
            WebSocketRoute("/v2/ws", ok_ws_endpoint, name="ws"),
        ]

        async def trigger(request: Request) -> JSONResponse:
            # Publish a whole new table from within a request: every lookup
            # below must still come from the version pinned at entry.
            request.app.publish_routes(v2_routes)
            return JSONResponse(
                {
                    "mount": str(request.url_for("api:users")),
                    "host": str(request.url_for("hostns:home")),
                    "websocket": str(request.url_for("ws")),
                    "version": request.scope["route_table"].version,
                }
            )

        v1_routes = [
            Route("/trigger", trigger),
            Mount("/api", routes=[Route("/users", ok_endpoint, name="users")], name="api"),
            Host(
                "api.example.com",
                app=Router(routes=[Route("/home", ok_endpoint, name="home")]),
                name="hostns",
            ),
            WebSocketRoute("/ws", ok_endpoint, name="ws"),
        ]
        app = Starlette(routes=v1_routes)

        status, _, body = await http_request(app, "/trigger")
        assert status == 200
        assert json.loads(body) == {
            "mount": "http://testserver/api/users",
            "host": "http://api.example.com/home",
            "websocket": "ws://testserver/ws",
            "version": 0,
        }

        # New requests resolve against the published v2 table.
        status, _, _ = await http_request(app, "/v2/api/users")
        assert status == 200
        status, _, _ = await http_request(app, "/api/users")
        assert status == 404
        status, _, _ = await http_request(app, "/v2/home", host="api.example.com")
        assert status == 200
        assert str(app.url_path_for("api:users")) == "/v2/api/users"

    asyncio.run(main())


def test_websocket_url_for_uses_pinned_version() -> None:
    async def main() -> None:
        v2_routes = [WebSocketRoute("/v2/ws", ok_ws_endpoint, name="ws")]

        async def ws_trigger(websocket: WebSocket) -> None:
            await websocket.accept()
            # Publish mid-connection; url_for() must keep the entry version.
            websocket.scope["app"].publish_routes(v2_routes)
            table = websocket.scope["route_table"]
            await websocket.send_text(f"v{table.version}:{websocket.url_for("ws")}")
            await websocket.close()

        app = Starlette(routes=[WebSocketRoute("/ws", ws_trigger, name="ws")])
        messages = await ws_request(app, "/ws")
        assert ws_texts(messages) == ["v0:ws://testserver/ws"]

        # New connections use the published table.
        messages = await ws_request(app, "/v2/ws")
        assert any(message["type"] == "websocket.accept" for message in messages)

    asyncio.run(main())


def test_url_404_405_and_websocket_results_traced_through_barrier() -> None:
    async def main() -> None:
        entered = AsyncBarrier(3)  # users endpoint, websocket endpoint, main task
        release = asyncio.Event()

        async def users_v1(request: Request) -> JSONResponse:
            await entered.wait()
            await release.wait()
            table = request.scope["route_table"]
            return JSONResponse({"version": table.version, "url": str(request.url_for("users"))})

        async def ws_v1(websocket: WebSocket) -> None:
            await websocket.accept()
            await entered.wait()
            await release.wait()
            table = websocket.scope["route_table"]
            await websocket.send_text(json.dumps({"version": table.version, "url": str(websocket.url_for("ws"))}))
            await websocket.close()

        async def users_v2(request: Request) -> JSONResponse:
            table = request.scope["route_table"]
            return JSONResponse({"version": table.version, "url": str(request.url_for("users"))})

        async def ws_v2(websocket: WebSocket) -> None:
            await websocket.accept()
            table = websocket.scope["route_table"]
            await websocket.send_text(json.dumps({"version": table.version, "url": str(websocket.url_for("ws"))}))
            await websocket.close()

        app = Starlette(
            routes=[
                Route("/users", users_v1, name="users"),
                Route("/submit", ok_endpoint, methods=["POST"], name="submit"),
                WebSocketRoute("/ws", ws_v1, name="ws"),
            ]
        )

        # Round 1: requests enter under v1 and rendezvous through the barrier.
        users_task = asyncio.create_task(http_request(app, "/users"))
        not_found_task = asyncio.create_task(http_request(app, "/missing"))
        method_task = asyncio.create_task(http_request(app, "/submit", method="GET"))
        ws_task = asyncio.create_task(ws_request(app, "/ws"))
        await entered.wait()

        # Publish v2 while the round-1 requests are still in flight.
        table = app.publish_routes(
            [
                Route("/v2/users", users_v2, name="users"),
                Route("/submit", ok_endpoint, methods=["GET"], name="submit"),
                WebSocketRoute("/v2/ws", ws_v2, name="ws"),
            ]
        )
        record_trace({"case": "publish", "from_version": 0, "to_version": table.version})
        release.set()

        users_result, not_found_result, method_result, ws_result = await asyncio.gather(
            users_task, not_found_task, method_task, ws_task
        )

        # In-flight requests keep version 0 for matching and url_for().
        assert users_result[0] == 200
        assert json.loads(users_result[2]) == {"version": 0, "url": "http://testserver/users"}
        record_trace(
            {"round": 1, "case": "url", "version": 0, "status": users_result[0], "url": "http://testserver/users"}
        )

        assert not_found_result[0] == 404
        record_trace({"round": 1, "case": "404", "version": 0, "status": 404})

        assert method_result[0] == 405
        assert method_result[1]["allow"] == "POST"
        record_trace({"round": 1, "case": "405", "version": 0, "status": 405, "allow": "POST"})

        assert json.loads(ws_texts(ws_result)[0]) == {"version": 0, "url": "ws://testserver/ws"}
        record_trace({"round": 1, "case": "ws", "version": 0, "url": "ws://testserver/ws"})

        # Round 2: requests entering after the publish observe version 1.
        users_result = await http_request(app, "/v2/users")
        assert users_result[0] == 200
        assert json.loads(users_result[2]) == {"version": 1, "url": "http://testserver/v2/users"}
        record_trace({"round": 2, "case": "url", "version": 1, "status": 200, "url": "http://testserver/v2/users"})

        not_found_result = await http_request(app, "/users")
        assert not_found_result[0] == 404
        record_trace({"round": 2, "case": "404", "version": 1, "status": 404})

        method_result = await http_request(app, "/submit", method="GET")
        assert method_result[0] == 200
        record_trace({"round": 2, "case": "405", "version": 1, "status": 200})

        ws_result = await ws_request(app, "/v2/ws")
        assert json.loads(ws_texts(ws_result)[0]) == {"version": 1, "url": "ws://testserver/v2/ws"}
        record_trace({"round": 2, "case": "ws", "version": 1, "url": "ws://testserver/v2/ws"})

    asyncio.run(main())


def test_concurrent_publishes_are_serialized_in_order() -> None:
    app = Starlette(routes=[Route("/ping", ok_endpoint, name="ping")])
    barrier = threading.Barrier(5)
    completed: list[tuple[int, int]] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        table = app.publish_routes([Route(f"/r{index}", ok_endpoint, name=f"r{index}")])
        with lock:
            completed.append((index, table.version))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Every publish got a distinct, strictly increasing version; the last
    # publication under the lock determines the final table.
    assert sorted(version for _, version in completed) == [1, 2, 3, 4, 5]
    assert app.route_table.version == 5
    final_index = next(index for index, version in completed if version == 5)
    assert [route.path for route in app.route_table.routes] == [f"/r{final_index}"]

    for order, (index, version) in enumerate(completed):
        record_trace({"case": "concurrent_publish", "order": order, "worker": index, "version": version})


def test_publish_does_not_serialize_in_flight_requests() -> None:
    async def main() -> None:
        barrier = AsyncBarrier(2)

        async def slow_a(request: Request) -> PlainTextResponse:
            await barrier.wait()
            return PlainTextResponse("a")

        async def slow_b(request: Request) -> PlainTextResponse:
            await barrier.wait()
            return PlainTextResponse("b")

        app = Starlette(routes=[Route("/a", slow_a), Route("/b", slow_b)])
        task_a = asyncio.create_task(http_request(app, "/a"))
        task_b = asyncio.create_task(http_request(app, "/b"))
        await asyncio.sleep(0.05)
        # Both requests are in flight inside their endpoints; publishing must
        # not wait for them, nor make them wait for each other. If request
        # handling were serialized the barrier would never be reached.
        app.publish_routes([Route("/c", ok_endpoint, name="c")])
        results = await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)
        assert [result[0] for result in results] == [200, 200]

    asyncio.run(main())


def test_failed_publish_keeps_old_table_and_version() -> None:
    async def main() -> None:
        app = Starlette(routes=[Route("/ok", ok_endpoint, name="ok")])
        before = app.route_table
        failures = []

        # 1. Unknown convertor in a candidate route path.
        class UpperConvertor(Convertor[str]):
            regex = "[A-Z]+"

            def convert(self, value: str) -> str:
                return value

        CONVERTOR_TYPES["upper"] = UpperConvertor()
        try:
            bad_convertor_route = Route("/x/{value:upper}", ok_endpoint)
        finally:
            del CONVERTOR_TYPES["upper"]
        with pytest.raises(RouteTableError, match="Unknown path convertor"):
            app.publish_routes([bad_convertor_route])
        failures.append({"case": "unknown_convertor", "version": app.route_table.version})

        # 2. Circular mount: the mounted router contains the mount itself.
        sub_router = Router()
        cyclic = Mount("/cyclic", app=sub_router)
        sub_router.routes = [cyclic]
        with pytest.raises(RouteTableError, match="Circular mount"):
            app.publish_routes([cyclic])
        failures.append({"case": "circular_mount", "version": app.route_table.version})

        # 3. Unacceptable name conflict: same name and parameters, different paths.
        conflicting = [
            Route("/a", ok_endpoint, name="dup"),
            Route("/b", ok_endpoint, name="dup"),
        ]
        with pytest.raises(RouteTableError, match="Conflicting route name"):
            app.publish_routes(conflicting)
        failures.append({"case": "name_conflict", "version": app.route_table.version})

        # 4. Entries that are not routes at all.
        with pytest.raises(RouteTableError, match="Expected a route instance"):
            app.publish_routes([123])
        failures.append({"case": "not_a_route", "version": app.route_table.version})

        # The failed publishes left the old table and its version untouched.
        assert app.route_table is before
        assert app.route_table.version == 0
        status, _, _ = await http_request(app, "/ok")
        assert status == 200

        # Rollback evidence is saved separately from the publish trace.
        save_rollback_evidence(
            {
                "test": "test_failed_publish_keeps_old_table_and_version",
                "version_before": before.version,
                "version_after": app.route_table.version,
                "table_preserved": app.route_table is before,
                "old_route_still_served": status == 200,
                "failures": failures,
            }
        )

    asyncio.run(main())
    assert ROLLBACK_FILE.exists()


def test_same_name_with_distinct_params_is_publishable() -> None:
    async def main() -> None:
        app = Starlette(routes=[])
        table = app.publish_routes(
            [
                Route("/user/{id:int}", ok_endpoint, name="user"),
                Route("/user/{name:str}", ok_endpoint, name="user"),
            ]
        )
        assert table.version == 1
        assert str(app.url_path_for("user", id=1)) == "/user/1"
        assert str(app.url_path_for("user", name="bob")) == "/user/bob"

    asyncio.run(main())
