from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from starlette.applications import Starlette
from starlette.lifespan import ComposedLifespan, composed_lifespan
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient
from tests.types import TestClientFactory


def make_app(name: str, events: list[str], *, fail_startup: bool = False, fail_shutdown: bool = False) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette):
        del app
        events.append(f"{name}.startup")
        try:
            if fail_startup:
                raise RuntimeError(f"{name}.startup")
            yield {"name": name, f"{name}_only": True}
        finally:
            events.append(f"{name}.shutdown")
            if fail_shutdown:
                raise RuntimeError(f"{name}.shutdown")

    async def endpoint(request: Request) -> JSONResponse:
        original_name = request.state.name
        request.state.name = f"{name}.mutated"
        return JSONResponse(
            {
                "name": original_name,
                "app_only": getattr(request.state, f"{name}_only", False),
                "root_only": getattr(request.state, "root_only", False),
            }
        )

    return Starlette(routes=[Route("/", endpoint)], lifespan=lifespan)


def test_composed_lifespan_starts_nested_mounts_in_route_order(
    test_client_factory: TestClientFactory,
) -> None:
    events: list[str] = []
    middle = make_app("middle", events)
    middle.router.routes.append(Mount("/deep", app=make_app("deep", events)))
    first = make_app("first", events)
    second = make_app("second", events)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        del app
        events.append("root.startup")
        try:
            yield
        finally:
            events.append("root.shutdown")

    app = Starlette(
        routes=[
            Mount("/first", app=first),
            Mount("/middle", app=middle),
            Mount("/second", app=second),
        ],
        lifespan=composed_lifespan(lifespan),
    )

    with test_client_factory(app):
        assert events == [
            "root.startup",
            "first.startup",
            "middle.startup",
            "deep.startup",
            "second.startup",
        ]

    assert events == [
        "root.startup",
        "first.startup",
        "middle.startup",
        "deep.startup",
        "second.startup",
        "second.shutdown",
        "deep.shutdown",
        "middle.shutdown",
        "first.shutdown",
        "root.shutdown",
    ]


def test_composed_lifespan_can_be_used_directly_as_context_manager(
    test_client_factory: TestClientFactory,
) -> None:
    events: list[str] = []
    child = make_app("child", events)
    app = Starlette(
        routes=[Mount("/child", app=child)],
        lifespan=lambda app: ComposedLifespan(app),
    )

    with test_client_factory(app):
        assert events == ["child.startup"]

    assert events == ["child.startup", "child.shutdown"]


def test_mounted_lifespans_are_not_started_by_default(test_client_factory: TestClientFactory) -> None:
    events: list[str] = []
    child = make_app("child", events)
    app = Starlette(routes=[Mount("/child", app=child)])

    with test_client_factory(app):
        assert events == []

    assert events == []


def test_startup_failure_rolls_back_only_entered_applications(
    test_client_factory: TestClientFactory,
) -> None:
    events: list[str] = []
    first = make_app("first", events)
    failing = make_app("failing", events, fail_startup=True)
    not_entered = make_app("not_entered", events)
    app = Starlette(
        routes=[
            Mount("/first", app=first),
            Mount("/failing", app=failing),
            Mount("/not-entered", app=not_entered),
        ],
        lifespan=composed_lifespan(),
    )

    with pytest.raises(RuntimeError, match="failing.startup"):
        with test_client_factory(app):
            pytest.fail("The application should not start")

    assert events == ["first.startup", "failing.startup", "failing.shutdown", "first.shutdown"]
    assert not_entered.router.composed_lifespan_state is None


def test_multiple_shutdown_failures_preserve_all_exceptions(test_client_factory: TestClientFactory) -> None:
    events: list[str] = []
    first = make_app("first", events, fail_shutdown=True)
    second = make_app("second", events, fail_shutdown=True)
    app = Starlette(
        routes=[Mount("/first", app=first), Mount("/second", app=second)],
        lifespan=composed_lifespan(),
    )

    with pytest.raises(BaseExceptionGroup) as exc_info:
        with test_client_factory(app):
            pass

    assert [str(exc) for exc in exc_info.value.exceptions] == ["second.shutdown", "first.shutdown"]
    assert events == [
        "first.startup",
        "second.startup",
        "second.shutdown",
        "first.shutdown",
    ]


def test_startup_and_rollback_failures_are_grouped(test_client_factory: TestClientFactory) -> None:
    events: list[str] = []
    first = make_app("first", events, fail_shutdown=True)
    failing = make_app("failing", events, fail_startup=True)
    app = Starlette(
        routes=[Mount("/first", app=first), Mount("/failing", app=failing)],
        lifespan=composed_lifespan(),
    )

    with pytest.raises(BaseExceptionGroup) as exc_info:
        with test_client_factory(app):
            pytest.fail("The application should not start")

    assert [str(exc) for exc in exc_info.value.exceptions] == ["failing.startup", "first.shutdown"]


def test_same_application_mounted_multiple_times_starts_once(
    test_client_factory: TestClientFactory,
) -> None:
    events: list[str] = []
    shared = make_app("shared", events)
    app = Starlette(
        routes=[Mount("/one", app=shared), Mount("/two", app=shared)],
        lifespan=composed_lifespan(),
    )

    with test_client_factory(app):
        assert events == ["shared.startup"]

    assert events == ["shared.startup", "shared.shutdown"]


def test_nested_composed_lifespan_starts_descendants_only_once(
    test_client_factory: TestClientFactory,
) -> None:
    events: list[str] = []
    grandchild = make_app("grandchild", events)

    @asynccontextmanager
    async def child_lifespan(app: Starlette):
        del app
        events.append("child.startup")
        try:
            yield
        finally:
            events.append("child.shutdown")

    child = Starlette(
        routes=[Mount("/grandchild", app=grandchild)],
        lifespan=lambda app: ComposedLifespan(app, child_lifespan),
    )
    app = Starlette(
        routes=[Mount("/child", app=child)],
        lifespan=composed_lifespan(),
    )

    with test_client_factory(app):
        assert events == ["child.startup", "grandchild.startup"]

    assert events == [
        "child.startup",
        "grandchild.startup",
        "grandchild.shutdown",
        "child.shutdown",
    ]


def test_cyclic_mount_fails_without_lifespan_side_effects() -> None:
    events: list[str] = []
    child = make_app("child", events)
    child.router.routes.append(Mount("/cycle", app=child))
    app = Starlette(
        routes=[Mount("/child", app=child)],
        lifespan=composed_lifespan(),
    )

    with pytest.raises(RuntimeError, match="cyclic Mount graph"):
        with TestClient(app):
            pytest.fail("The application should not start")

    assert events == []


def test_request_state_is_isolated_per_mounted_application(
    test_client_factory: TestClientFactory,
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def root_lifespan(app: Starlette):
        del app
        yield {"name": "root", "root_only": True}

    async def root_endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"name": request.state.name, "child_only": getattr(request.state, "child_only", False)})

    child = make_app("child", events)
    sibling = make_app("sibling", events)
    deep = make_app("deep", events)
    middle = Starlette(routes=[Mount("/deep", app=deep)])
    app = Starlette(
        routes=[
            Route("/", root_endpoint),
            Mount("/child", app=child),
            Mount("/middle", app=middle),
            Mount("/sibling", app=sibling),
        ],
        lifespan=composed_lifespan(root_lifespan),
    )

    with test_client_factory(app) as client:
        assert client.get("/").json() == {"name": "root", "child_only": False}
        assert client.get("/child/").json() == {
            "name": "child",
            "app_only": True,
            "root_only": True,
        }
        assert client.get("/sibling/").json() == {
            "name": "sibling",
            "app_only": True,
            "root_only": True,
        }
        assert client.get("/middle/deep/").json() == {
            "name": "deep",
            "app_only": True,
            "root_only": True,
        }
        assert client.get("/sibling/").json()["name"] == "sibling"
        assert client.get("/").json() == {"name": "root", "child_only": False}
