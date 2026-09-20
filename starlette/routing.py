from __future__ import annotations

import contextlib
import functools
import inspect
import re
import threading
import traceback
import types
import warnings
from collections.abc import Awaitable, Collection, Generator, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager, asynccontextmanager
from enum import Enum
from re import Pattern
from typing import Any, Callable, TypeVar

from starlette._exception_handler import wrap_app_handling_exceptions
from starlette._utils import get_route_path, is_async_callable
from starlette.concurrency import run_in_threadpool
from starlette.convertors import CONVERTOR_TYPES, Convertor
from starlette.datastructures import URL, Headers, URLPath
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Lifespan, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketClose


class NoMatchFound(Exception):
    """
    Raised by `.url_for(name, **path_params)` and `.url_path_for(name, **path_params)`
    if no matching route exists.
    """

    def __init__(self, name: str, path_params: dict[str, Any]) -> None:
        params = ", ".join(list(path_params.keys()))
        super().__init__(f'No route exists for name "{name}" and params "{params}".')


class RouteTableError(ValueError):
    """
    Raised by `Router.publish_routes()` when a candidate route table fails
    validation. The publish is aborted atomically: the previously published
    table and its version number are left untouched.
    """


class Match(Enum):
    NONE = 0
    PARTIAL = 1
    FULL = 2


def iscoroutinefunction_or_partial(obj: Any) -> bool:  # pragma: no cover
    """
    Correctly determines if an object is a coroutine function,
    including those wrapped in functools.partial objects.
    """
    warnings.warn(
        "iscoroutinefunction_or_partial is deprecated, and will be removed in a future release.",
        DeprecationWarning,
    )
    while isinstance(obj, functools.partial):
        obj = obj.func
    return inspect.iscoroutinefunction(obj)


def request_response(
    func: Callable[[Request], Awaitable[Response] | Response],
) -> ASGIApp:
    """
    Takes a function or coroutine `func(request) -> response`,
    and returns an ASGI application.
    """
    f: Callable[[Request], Awaitable[Response]] = (
        func if is_async_callable(func) else functools.partial(run_in_threadpool, func)
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive, send)

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            response = await f(request)
            await response(scope, receive, send)

        await wrap_app_handling_exceptions(app, request)(scope, receive, send)

    return app


def websocket_session(
    func: Callable[[WebSocket], Awaitable[None]],
) -> ASGIApp:
    """
    Takes a coroutine `func(session)`, and returns an ASGI application.
    """
    # assert asyncio.iscoroutinefunction(func), "WebSocket endpoints must be async"

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        session = WebSocket(scope, receive=receive, send=send)

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await func(session)

        await wrap_app_handling_exceptions(app, session)(scope, receive, send)

    return app


def get_name(endpoint: Callable[..., Any]) -> str:
    return getattr(endpoint, "__name__", endpoint.__class__.__name__)


def replace_params(
    path: str,
    param_convertors: dict[str, Convertor[Any]],
    path_params: dict[str, str],
) -> tuple[str, dict[str, str]]:
    for key, value in list(path_params.items()):
        if "{" + key + "}" in path:
            convertor = param_convertors[key]
            value = convertor.to_string(value)
            path = path.replace("{" + key + "}", value)
            path_params.pop(key)
    return path, path_params


# Match parameters in URL paths, eg. '{param}', and '{param:int}'
PARAM_REGEX = re.compile("{([a-zA-Z_][a-zA-Z0-9_]*)(:[a-zA-Z_][a-zA-Z0-9_]*)?}")


def compile_path(
    path: str,
) -> tuple[Pattern[str], str, dict[str, Convertor[Any]]]:
    """
    Given a path string, like: "/{username:str}",
    or a host string, like: "{subdomain}.mydomain.org", return a three-tuple
    of (regex, format, {param_name:convertor}).

    regex:      "/(?P<username>[^/]+)"
    format:     "/{username}"
    convertors: {"username": StringConvertor()}
    """
    is_host = not path.startswith("/")

    path_regex = "^"
    path_format = ""
    duplicated_params = set()

    idx = 0
    param_convertors = {}
    for match in PARAM_REGEX.finditer(path):
        param_name, convertor_type = match.groups("str")
        convertor_type = convertor_type.lstrip(":")
        assert convertor_type in CONVERTOR_TYPES, f"Unknown path convertor '{convertor_type}'"
        convertor = CONVERTOR_TYPES[convertor_type]

        path_regex += re.escape(path[idx : match.start()])
        path_regex += f"(?P<{param_name}>{convertor.regex})"

        path_format += path[idx : match.start()]
        path_format += "{%s}" % param_name

        if param_name in param_convertors:
            duplicated_params.add(param_name)

        param_convertors[param_name] = convertor

        idx = match.end()

    if duplicated_params:
        names = ", ".join(sorted(duplicated_params))
        ending = "s" if len(duplicated_params) > 1 else ""
        raise ValueError(f"Duplicated param name{ending} {names} at path {path}")

    if is_host:
        # Align with `Host.matches()` behavior, which ignores port.
        hostname = path[idx:].split(":")[0]
        path_regex += re.escape(hostname) + "$"
    else:
        path_regex += re.escape(path[idx:]) + "$"

    path_format += path[idx:]

    return re.compile(path_regex), path_format, param_convertors


class BaseRoute:
    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        raise NotImplementedError()  # pragma: no cover

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        raise NotImplementedError()  # pragma: no cover

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise NotImplementedError()  # pragma: no cover

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        A route may be used in isolation as a stand-alone ASGI app.
        This is a somewhat contrived case, as they'll almost always be used
        within a Router, but could be useful for some tooling and minimal apps.
        """
        match, child_scope = self.matches(scope)
        if match == Match.NONE:
            if scope["type"] == "http":
                response = PlainTextResponse("Not Found", status_code=404)
                await response(scope, receive, send)
            elif scope["type"] == "websocket":  # pragma: no branch
                websocket_close = WebSocketClose()
                await websocket_close(scope, receive, send)
            return

        scope.update(child_scope)
        await self.handle(scope, receive, send)


class Route(BaseRoute):
    def __init__(
        self,
        path: str,
        endpoint: Callable[..., Any],
        *,
        methods: Collection[str] | None = None,
        name: str | None = None,
        include_in_schema: bool = True,
        middleware: Sequence[Middleware] | None = None,
    ) -> None:
        assert path.startswith("/"), "Routed paths must start with '/'"
        self.path = path
        self.endpoint = endpoint
        self.name = get_name(endpoint) if name is None else name
        self.include_in_schema = include_in_schema

        endpoint_handler = endpoint
        while isinstance(endpoint_handler, functools.partial):
            endpoint_handler = endpoint_handler.func
        if inspect.isfunction(endpoint_handler) or inspect.ismethod(endpoint_handler):
            # Endpoint is function or method. Treat it as `func(request) -> response`.
            self.app = request_response(endpoint)
            if methods is None:
                methods = ["GET"]
        else:
            # Endpoint is a class. Treat it as ASGI.
            self.app = endpoint

        if middleware is not None:
            for cls, args, kwargs in reversed(middleware):
                self.app = cls(self.app, *args, **kwargs)

        if methods is None:
            self.methods = None
        else:
            self.methods = {method.upper() for method in methods}
            if "GET" in self.methods:
                self.methods.add("HEAD")

        self.path_regex, self.path_format, self.param_convertors = compile_path(path)

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        path_params: dict[str, Any]
        if scope["type"] == "http":
            route_path = get_route_path(scope)
            match = self.path_regex.match(route_path)
            if match:
                matched_params = match.groupdict()
                for key, value in matched_params.items():
                    matched_params[key] = self.param_convertors[key].convert(value)
                path_params = dict(scope.get("path_params", {}))
                path_params.update(matched_params)
                child_scope = {"endpoint": self.endpoint, "path_params": path_params}
                if self.methods and scope["method"] not in self.methods:
                    return Match.PARTIAL, child_scope
                else:
                    return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        seen_params = set(path_params.keys())
        expected_params = set(self.param_convertors.keys())

        if name != self.name or seen_params != expected_params:
            raise NoMatchFound(name, path_params)

        path, remaining_params = replace_params(self.path_format, self.param_convertors, path_params)
        assert not remaining_params
        return URLPath(path=path, protocol="http")

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.methods and scope["method"] not in self.methods:
            headers = {"Allow": ", ".join(self.methods)}
            if "app" in scope:
                raise HTTPException(status_code=405, headers=headers)
            else:
                response = PlainTextResponse("Method Not Allowed", status_code=405, headers=headers)
            await response(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, Route)
            and self.path == other.path
            and self.endpoint == other.endpoint
            and self.methods == other.methods
        )

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        methods = sorted(self.methods or [])
        path, name = self.path, self.name
        return f"{class_name}(path={path!r}, name={name!r}, methods={methods!r})"


class WebSocketRoute(BaseRoute):
    def __init__(
        self,
        path: str,
        endpoint: Callable[..., Any],
        *,
        name: str | None = None,
        middleware: Sequence[Middleware] | None = None,
    ) -> None:
        assert path.startswith("/"), "Routed paths must start with '/'"
        self.path = path
        self.endpoint = endpoint
        self.name = get_name(endpoint) if name is None else name

        endpoint_handler = endpoint
        while isinstance(endpoint_handler, functools.partial):
            endpoint_handler = endpoint_handler.func
        if inspect.isfunction(endpoint_handler) or inspect.ismethod(endpoint_handler):
            # Endpoint is function or method. Treat it as `func(websocket)`.
            self.app = websocket_session(endpoint)
        else:
            # Endpoint is a class. Treat it as ASGI.
            self.app = endpoint

        if middleware is not None:
            for cls, args, kwargs in reversed(middleware):
                self.app = cls(self.app, *args, **kwargs)

        self.path_regex, self.path_format, self.param_convertors = compile_path(path)

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        path_params: dict[str, Any]
        if scope["type"] == "websocket":
            route_path = get_route_path(scope)
            match = self.path_regex.match(route_path)
            if match:
                matched_params = match.groupdict()
                for key, value in matched_params.items():
                    matched_params[key] = self.param_convertors[key].convert(value)
                path_params = dict(scope.get("path_params", {}))
                path_params.update(matched_params)
                child_scope = {"endpoint": self.endpoint, "path_params": path_params}
                return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        seen_params = set(path_params.keys())
        expected_params = set(self.param_convertors.keys())

        if name != self.name or seen_params != expected_params:
            raise NoMatchFound(name, path_params)

        path, remaining_params = replace_params(self.path_format, self.param_convertors, path_params)
        assert not remaining_params
        return URLPath(path=path, protocol="websocket")

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, WebSocketRoute) and self.path == other.path and self.endpoint == other.endpoint

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(path={self.path!r}, name={self.name!r})"


class Mount(BaseRoute):
    def __init__(
        self,
        path: str,
        app: ASGIApp | None = None,
        routes: Sequence[BaseRoute] | None = None,
        name: str | None = None,
        *,
        middleware: Sequence[Middleware] | None = None,
    ) -> None:
        assert path == "" or path.startswith("/"), "Routed paths must start with '/'"
        assert app is not None or routes is not None, "Either 'app=...', or 'routes=' must be specified"
        self.path = path.rstrip("/")
        if app is not None:
            self._base_app: ASGIApp = app
        else:
            self._base_app = Router(routes=routes)
        self.app = self._base_app
        if middleware is not None:
            for cls, args, kwargs in reversed(middleware):
                self.app = cls(self.app, *args, **kwargs)
        self.name = name
        self.path_regex, self.path_format, self.param_convertors = compile_path(self.path + "/{path:path}")

    @property
    def routes(self) -> list[BaseRoute]:
        return getattr(self._base_app, "routes", [])

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        path_params: dict[str, Any]
        if scope["type"] in ("http", "websocket"):  # pragma: no branch
            root_path = scope.get("root_path", "")
            route_path = get_route_path(scope)
            match = self.path_regex.match(route_path)
            if match:
                matched_params = match.groupdict()
                for key, value in matched_params.items():
                    matched_params[key] = self.param_convertors[key].convert(value)
                remaining_path = "/" + matched_params.pop("path")
                matched_path = route_path[: -len(remaining_path)]
                path_params = dict(scope.get("path_params", {}))
                path_params.update(matched_params)
                child_scope = {
                    "path_params": path_params,
                    # app_root_path will only be set at the top level scope,
                    # initialized with the (optional) value of a root_path
                    # set above/before Starlette. And even though any
                    # mount will have its own child scope with its own respective
                    # root_path, the app_root_path will always be available in all
                    # the child scopes with the same top level value because it's
                    # set only once here with a default, any other child scope will
                    # just inherit that app_root_path default value stored in the
                    # scope. All this is needed to support Request.url_for(), as it
                    # uses the app_root_path to build the URL path.
                    "app_root_path": scope.get("app_root_path", root_path),
                    "root_path": root_path + matched_path,
                    "endpoint": self.app,
                }
                return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        if self.name is not None and name == self.name and "path" in path_params:
            # 'name' matches "<mount_name>".
            path_params["path"] = path_params["path"].lstrip("/")
            path, remaining_params = replace_params(self.path_format, self.param_convertors, path_params)
            if not remaining_params:
                return URLPath(path=path)
        elif self.name is None or name.startswith(self.name + ":"):
            if self.name is None:
                # No mount name.
                remaining_name = name
            else:
                # 'name' matches "<mount_name>:<child_name>".
                remaining_name = name[len(self.name) + 1 :]
            path_kwarg = path_params.get("path")
            path_params["path"] = ""
            path_prefix, remaining_params = replace_params(self.path_format, self.param_convertors, path_params)
            if path_kwarg is not None:
                remaining_params["path"] = path_kwarg
            for route in self.routes or []:
                try:
                    url = route.url_path_for(remaining_name, **remaining_params)
                    return URLPath(path=path_prefix.rstrip("/") + str(url), protocol=url.protocol)
                except NoMatchFound:
                    pass
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Mount) and self.path == other.path and self.app == other.app

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        name = self.name or ""
        return f"{class_name}(path={self.path!r}, name={name!r}, app={self.app!r})"


class Host(BaseRoute):
    def __init__(self, host: str, app: ASGIApp, name: str | None = None) -> None:
        assert not host.startswith("/"), "Host must not start with '/'"
        self.host = host
        self.app = app
        self.name = name
        self.host_regex, self.host_format, self.param_convertors = compile_path(host)

    @property
    def routes(self) -> list[BaseRoute]:
        return getattr(self.app, "routes", [])

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] in ("http", "websocket"):  # pragma:no branch
            headers = Headers(scope=scope)
            host = headers.get("host", "").split(":")[0]
            match = self.host_regex.match(host)
            if match:
                matched_params = match.groupdict()
                for key, value in matched_params.items():
                    matched_params[key] = self.param_convertors[key].convert(value)
                path_params = dict(scope.get("path_params", {}))
                path_params.update(matched_params)
                child_scope = {"path_params": path_params, "endpoint": self.app}
                return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        if self.name is not None and name == self.name and "path" in path_params:
            # 'name' matches "<mount_name>".
            path = path_params.pop("path")
            host, remaining_params = replace_params(self.host_format, self.param_convertors, path_params)
            if not remaining_params:
                return URLPath(path=path, host=host)
        elif self.name is None or name.startswith(self.name + ":"):
            if self.name is None:
                # No mount name.
                remaining_name = name
            else:
                # 'name' matches "<mount_name>:<child_name>".
                remaining_name = name[len(self.name) + 1 :]
            host, remaining_params = replace_params(self.host_format, self.param_convertors, path_params)
            for route in self.routes or []:
                try:
                    url = route.url_path_for(remaining_name, **remaining_params)
                    return URLPath(path=str(url), protocol=url.protocol, host=host)
                except NoMatchFound:
                    pass
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Host) and self.host == other.host and self.app == other.app

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        name = self.name or ""
        return f"{class_name}(host={self.host!r}, name={name!r}, app={self.app!r})"


_T = TypeVar("_T")


class _AsyncLiftContextManager(AbstractAsyncContextManager[_T]):
    def __init__(self, cm: AbstractContextManager[_T]):
        self._cm = cm

    async def __aenter__(self) -> _T:
        return self._cm.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> bool | None:
        return self._cm.__exit__(exc_type, exc_value, traceback)


def _wrap_gen_lifespan_context(
    lifespan_context: Callable[[Any], Generator[Any, Any, Any]],
) -> Callable[[Any], AbstractAsyncContextManager[Any]]:
    cmgr = contextlib.contextmanager(lifespan_context)

    @functools.wraps(cmgr)
    def wrapper(app: Any) -> _AsyncLiftContextManager[Any]:
        return _AsyncLiftContextManager(cmgr(app))

    return wrapper


class _DefaultLifespan:
    def __init__(self, router: Router):
        self._router = router

    async def __aenter__(self) -> None:
        await self._router.startup()

    async def __aexit__(self, *exc_info: object) -> None:
        await self._router.shutdown()

    def __call__(self: _T, app: object) -> _T:
        return self


class RouteTable:
    """
    An immutable, versioned snapshot of a router's routes.

    A route table is the unit of atomic publication: `Router.publish_routes()`
    validates a candidate table and swaps it in as a whole, so any request that
    is already being routed keeps using the table version it entered with,
    while new requests observe the newly published version.

    The snapshot holds its own copy of the routes, so mutating the list that
    was passed to `publish_routes()` afterwards cannot alter the published
    version.
    """

    __slots__ = ("routes", "version")

    def __init__(self, routes: Sequence[BaseRoute], version: int) -> None:
        self.routes = tuple(routes)
        self.version = version

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        for route in self.routes:
            try:
                return route.url_path_for(name, **path_params)
            except NoMatchFound:
                pass
        raise NoMatchFound(name, path_params)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, RouteTable) and self.routes == other.routes and self.version == other.version

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(version={self.version}, routes={list(self.routes)!r})"


def _check_name_conflicts(
    routes: Sequence[BaseRoute], seen: dict[tuple[str, frozenset[str]], str], namespace: str
) -> None:
    for route in routes:
        if isinstance(route, (Mount, Host)):
            if route.name is not None:
                # A named Mount/Host introduces its own namespace, addressed
                # as "<name>:<child_name>". Names inside it cannot conflict
                # with names outside of it.
                _check_name_conflicts(route.routes, {}, namespace=f"{namespace}{route.name}:")
            else:
                # An unnamed Mount/Host merges its children into the
                # surrounding namespace.
                _check_name_conflicts(route.routes, seen, namespace)
        elif isinstance(route, (Route, WebSocketRoute)):
            # Two leaf routes in the same namespace that share a name and the
            # same set of path parameters but have different paths cannot both
            # be reversed correctly (the first one always wins), which is an
            # unacceptable name conflict for publication. Routes sharing a
            # name with distinct parameters are unambiguous, as `url_for()`
            # selects them by the given keyword arguments.
            key = (route.name, frozenset(route.param_convertors))
            existing = seen.get(key)
            if existing is not None and existing != route.path_format:
                raise RouteTableError(
                    f'Conflicting route name "{namespace}{route.name}": routes with the same name and path '
                    "parameters but different paths cannot be published, as `url_for()` would be ambiguous."
                )
            seen[key] = route.path_format


def _check_route_cycles(routes: Sequence[BaseRoute], active: set[int]) -> None:
    for route in routes:
        if isinstance(route, (Mount, Host)):
            if id(route) in active:
                raise RouteTableError(
                    f"Circular mount detected: {route!r} contains itself, either directly or transitively."
                )
            active.add(id(route))
            _check_route_cycles(route.routes, active)
            active.discard(id(route))


def _check_route_paths(routes: Sequence[BaseRoute], seen: set[int]) -> None:
    for route in routes:
        if id(route) in seen:
            continue
        seen.add(id(route))
        try:
            if isinstance(route, (Route, WebSocketRoute)):
                compile_path(route.path)
            elif isinstance(route, Mount):
                compile_path(route.path + "/{path:path}")
            elif isinstance(route, Host):
                compile_path(route.host)
        except (AssertionError, ValueError) as exc:
            raise RouteTableError(f"Invalid route path for {route!r}: {exc}") from exc
        if isinstance(route, (Mount, Host)):
            _check_route_paths(route.routes, seen)


def validate_route_table(routes: Sequence[BaseRoute]) -> None:
    """
    Validate a candidate route table prior to publication.

    Raises `RouteTableError` if any entry is not a route, if a path or host
    uses an unknown convertor or duplicated parameter names, if mounts or
    hosts form a cycle, or if routes within the same namespace have
    unacceptable name conflicts. Raises nothing if the table is publishable.
    """
    for route in routes:
        if not isinstance(route, BaseRoute):
            raise RouteTableError(f"Expected a route instance, got {route!r}.")
    _check_route_cycles(routes, set())
    _check_route_paths(routes, set())
    _check_name_conflicts(routes, {}, "")


class Router:
    def __init__(
        self,
        routes: Sequence[BaseRoute] | None = None,
        redirect_slashes: bool = True,
        default: ASGIApp | None = None,
        on_startup: Sequence[Callable[[], Any]] | None = None,
        on_shutdown: Sequence[Callable[[], Any]] | None = None,
        # the generic to Lifespan[AppType] is the type of the top level application
        # which the router cannot know statically, so we use Any
        lifespan: Lifespan[Any] | None = None,
        *,
        middleware: Sequence[Middleware] | None = None,
    ) -> None:
        # The currently published route table. Requests pin the table version
        # that is current when they enter the router, so an in-flight request
        # keeps matching, redirecting and reversing URLs against one version
        # even if a new table is published concurrently.
        self._route_table = RouteTable([] if routes is None else list(routes), version=0)
        # Serializes concurrent publications. Request handling never takes
        # this lock, so publishing never serializes in-flight requests.
        self._publish_lock = threading.Lock()
        self.redirect_slashes = redirect_slashes
        self.default = self.not_found if default is None else default
        self.on_startup = [] if on_startup is None else list(on_startup)
        self.on_shutdown = [] if on_shutdown is None else list(on_shutdown)

        if on_startup or on_shutdown:
            warnings.warn(
                "The on_startup and on_shutdown parameters are deprecated, and they "
                "will be removed on version 1.0. Use the lifespan parameter instead. "
                "See more about it on https://www.starlette.io/lifespan/.",
                DeprecationWarning,
            )
            if lifespan:
                warnings.warn(
                    "The `lifespan` parameter cannot be used with `on_startup` or "
                    "`on_shutdown`. Both `on_startup` and `on_shutdown` will be "
                    "ignored."
                )

        if lifespan is None:
            self.lifespan_context: Lifespan[Any] = _DefaultLifespan(self)

        elif inspect.isasyncgenfunction(lifespan):
            warnings.warn(
                "async generator function lifespans are deprecated, "
                "use an @contextlib.asynccontextmanager function instead",
                DeprecationWarning,
            )
            self.lifespan_context = asynccontextmanager(
                lifespan,
            )
        elif inspect.isgeneratorfunction(lifespan):
            warnings.warn(
                "generator function lifespans are deprecated, use an @contextlib.asynccontextmanager function instead",
                DeprecationWarning,
            )
            self.lifespan_context = _wrap_gen_lifespan_context(
                lifespan,
            )
        else:
            self.lifespan_context = lifespan

        self.middleware_stack = self.app
        if middleware:
            for cls, args, kwargs in reversed(middleware):
                self.middleware_stack = cls(self.middleware_stack, *args, **kwargs)

    @property
    def routes(self) -> list[BaseRoute]:
        return list(self._route_table.routes)

    @routes.setter
    def routes(self, routes: Sequence[BaseRoute]) -> None:
        # Replacing the routes outside of `publish_routes()` is treated as an
        # unvalidated, copy-on-write update, preserving the historical
        # behavior of mutating `router.routes` directly.
        with self._publish_lock:
            self._route_table = RouteTable(list(routes), version=self._route_table.version + 1)

    @property
    def route_table(self) -> RouteTable:
        """The currently published route table snapshot."""
        return self._route_table

    def publish_routes(self, routes: Sequence[BaseRoute]) -> RouteTable:
        """
        Validate and atomically publish a new route table.

        The candidate table is fully validated first. If validation fails a
        `RouteTableError` is raised and the previously published table and
        its version number are left untouched. On success the new table is
        swapped in as a whole: requests that enter the router afterwards use
        the new version, while requests already in flight keep the version
        they entered with.

        The candidate routes are copied, so later mutations of the list
        passed in cannot alter the published version. Concurrent calls are
        serialized and each successful call publishes a strictly increasing
        version number.
        """
        candidate = list(routes)
        with self._publish_lock:
            validate_route_table(candidate)
            table = RouteTable(candidate, version=self._route_table.version + 1)
            self._route_table = table
        return table

    async def not_found(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            websocket_close = WebSocketClose()
            await websocket_close(scope, receive, send)
            return

        # If we're running inside a starlette application then raise an
        # exception, so that the configurable exception handler can deal with
        # returning the response. For plain ASGI apps, just return the response.
        if "app" in scope:
            raise HTTPException(status_code=404)
        else:
            response = PlainTextResponse("Not Found", status_code=404)
        await response(scope, receive, send)

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        return self._route_table.url_path_for(name, **path_params)

    async def startup(self) -> None:
        """
        Run any `.on_startup` event handlers.
        """
        for handler in self.on_startup:
            if is_async_callable(handler):
                await handler()
            else:
                handler()

    async def shutdown(self) -> None:
        """
        Run any `.on_shutdown` event handlers.
        """
        for handler in self.on_shutdown:
            if is_async_callable(handler):
                await handler()
            else:
                handler()

    async def lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Handle ASGI lifespan messages, which allows us to manage application
        startup and shutdown events.
        """
        started = False
        app: Any = scope.get("app")
        await receive()
        try:
            async with self.lifespan_context(app) as maybe_state:
                if maybe_state is not None:
                    if "state" not in scope:
                        raise RuntimeError('The server does not support "state" in the lifespan scope.')
                    scope["state"].update(maybe_state)
                await send({"type": "lifespan.startup.complete"})
                started = True
                await receive()
        except BaseException:
            exc_text = traceback.format_exc()
            if started:
                await send({"type": "lifespan.shutdown.failed", "message": exc_text})
            else:
                await send({"type": "lifespan.startup.failed", "message": exc_text})
            raise
        else:
            await send({"type": "lifespan.shutdown.complete"})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        The main entry point to the Router class.
        """
        await self.middleware_stack(scope, receive, send)

    async def app(self, scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["type"] in ("http", "websocket", "lifespan")

        if "router" not in scope:
            scope["router"] = self

        if scope["type"] == "lifespan":
            await self.lifespan(scope, receive, send)
            return

        # Pin the route table version for this request. Matching, trailing
        # slash redirects and `url_for()` within a single request all use
        # the version that was current when the request entered, even if a
        # new table is published while the request is in flight. The
        # outermost router's table governs `url_for()` for the request.
        route_table = self._route_table
        scope.setdefault("route_table", route_table)
        routes = route_table.routes

        partial = None

        for route in routes:
            # Determine if any route matches the incoming scope,
            # and hand over to the matching route if found.
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                scope.update(child_scope)
                await route.handle(scope, receive, send)
                return
            elif match == Match.PARTIAL and partial is None:
                partial = route
                partial_scope = child_scope

        if partial is not None:
            #  Handle partial matches. These are cases where an endpoint is
            # able to handle the request, but is not a preferred option.
            # We use this in particular to deal with "405 Method Not Allowed".
            scope.update(partial_scope)
            await partial.handle(scope, receive, send)
            return

        route_path = get_route_path(scope)
        if scope["type"] == "http" and self.redirect_slashes and route_path != "/":
            redirect_scope = dict(scope)
            if route_path.endswith("/"):
                redirect_scope["path"] = redirect_scope["path"].rstrip("/")
            else:
                redirect_scope["path"] = redirect_scope["path"] + "/"

            for route in routes:
                match, child_scope = route.matches(redirect_scope)
                if match != Match.NONE:
                    redirect_url = URL(scope=redirect_scope)
                    response = RedirectResponse(url=str(redirect_url))
                    await response(scope, receive, send)
                    return

        await self.default(scope, receive, send)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Router) and self.routes == other.routes

    def mount(self, path: str, app: ASGIApp, name: str | None = None) -> None:  # pragma: no cover
        route = Mount(path, app=app, name=name)
        self.routes = [*self.routes, route]

    def host(self, host: str, app: ASGIApp, name: str | None = None) -> None:  # pragma: no cover
        route = Host(host, app=app, name=name)
        self.routes = [*self.routes, route]

    def add_route(
        self,
        path: str,
        endpoint: Callable[[Request], Awaitable[Response] | Response],
        methods: Collection[str] | None = None,
        name: str | None = None,
        include_in_schema: bool = True,
    ) -> None:  # pragma: no cover
        route = Route(
            path,
            endpoint=endpoint,
            methods=methods,
            name=name,
            include_in_schema=include_in_schema,
        )
        self.routes = [*self.routes, route]

    def add_websocket_route(
        self,
        path: str,
        endpoint: Callable[[WebSocket], Awaitable[None]],
        name: str | None = None,
    ) -> None:  # pragma: no cover
        route = WebSocketRoute(path, endpoint=endpoint, name=name)
        self.routes = [*self.routes, route]

    def route(
        self,
        path: str,
        methods: Collection[str] | None = None,
        name: str | None = None,
        include_in_schema: bool = True,
    ) -> Callable:  # type: ignore[type-arg]
        """
        We no longer document this decorator style API, and its usage is discouraged.
        Instead you should use the following approach:

        >>> routes = [Route(path, endpoint=...), ...]
        >>> app = Starlette(routes=routes)
        """
        warnings.warn(
            "The `route` decorator is deprecated, and will be removed in version 1.0.0."
            "Refer to https://www.starlette.io/routing/#http-routing for the recommended approach.",
            DeprecationWarning,
        )

        def decorator(func: Callable) -> Callable:  # type: ignore[type-arg]
            self.add_route(
                path,
                func,
                methods=methods,
                name=name,
                include_in_schema=include_in_schema,
            )
            return func

        return decorator

    def websocket_route(self, path: str, name: str | None = None) -> Callable:  # type: ignore[type-arg]
        """
        We no longer document this decorator style API, and its usage is discouraged.
        Instead you should use the following approach:

        >>> routes = [WebSocketRoute(path, endpoint=...), ...]
        >>> app = Starlette(routes=routes)
        """
        warnings.warn(
            "The `websocket_route` decorator is deprecated, and will be removed in version 1.0.0. Refer to "
            "https://www.starlette.io/routing/#websocket-routing for the recommended approach.",
            DeprecationWarning,
        )

        def decorator(func: Callable) -> Callable:  # type: ignore[type-arg]
            self.add_websocket_route(path, func, name=name)
            return func

        return decorator

    def add_event_handler(self, event_type: str, func: Callable[[], Any]) -> None:  # pragma: no cover
        assert event_type in ("startup", "shutdown")

        if event_type == "startup":
            self.on_startup.append(func)
        else:
            self.on_shutdown.append(func)

    def on_event(self, event_type: str) -> Callable:  # type: ignore[type-arg]
        warnings.warn(
            "The `on_event` decorator is deprecated, and will be removed in version 1.0.0. "
            "Refer to https://www.starlette.io/lifespan/ for recommended approach.",
            DeprecationWarning,
        )

        def decorator(func: Callable) -> Callable:  # type: ignore[type-arg]
            self.add_event_handler(event_type, func)
            return func

        return decorator
