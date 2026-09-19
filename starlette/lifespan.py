from __future__ import annotations

import contextlib
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import TracebackType
from typing import TYPE_CHECKING, Any, cast

from starlette.routing import Mount
from starlette.types import Lifespan

if TYPE_CHECKING:
    from starlette.routing import Router

if sys.version_info >= (3, 11):
    BaseExceptionGroup = BaseExceptionGroup
else:  # pragma: no cover
    try:
        from exceptiongroup import BaseExceptionGroup
    except ImportError:
        BaseExceptionGroup = None


def _collect_mounted_applications(root_router: Router) -> list[Any]:
    applications: list[Any] = []
    seen_applications: set[int] = set()
    active_routers: set[int] = set()

    def visit_router(router: Any, managed: bool = False) -> None:
        router_id = id(router)
        if router_id in active_routers:
            raise RuntimeError("Cannot compose lifespans for a cyclic Mount graph.")

        active_routers.add(router_id)
        try:
            for route in router.routes:
                if not isinstance(route, Mount):
                    continue

                application = route._base_app
                application_router = getattr(application, "router", None)
                if application_router is not None and hasattr(application_router, "routes"):
                    application_id = id(application)
                    manages_descendants = getattr(
                        application_router.lifespan_context,
                        "_starlette_composed_lifespan",
                        False,
                    )
                    if not managed and application_id not in seen_applications:
                        seen_applications.add(application_id)
                        applications.append(application)
                    visit_router(application_router, managed or manages_descendants)
                elif hasattr(application, "routes"):
                    visit_router(application, managed)
        finally:
            active_routers.remove(router_id)

    visit_router(root_router)
    return applications


class ComposedLifespan(AbstractAsyncContextManager[Mapping[str, Any] | None]):
    _starlette_composed_lifespan = True

    def __init__(self, app: Any, lifespan: Lifespan[Any] | None = None) -> None:
        self._app = app
        self._lifespan = lifespan

    async def __aenter__(self) -> Mapping[str, Any] | None:
        root_router = getattr(self._app, "router", None)
        if root_router is None or not hasattr(root_router, "routes"):
            root_router = self._app
        if not hasattr(root_router, "routes"):
            raise RuntimeError("ComposedLifespan requires a Starlette application or Router.")
        self._root_router = root_router

        applications = _collect_mounted_applications(root_router)
        self._entries: list[tuple[Any, AbstractAsyncContextManager[Any], dict[str, Any]]] = []

        try:
            root_state: dict[str, Any] = {}
            parent_context: AbstractAsyncContextManager[Any]
            if self._lifespan is None:
                parent_context = contextlib.nullcontext()
            else:
                parent_context = self._lifespan(self._app)

            maybe_state = await parent_context.__aenter__()
            self._entries.append((self._app, parent_context, root_state))

            if maybe_state is not None:
                root_state.update(maybe_state)
                root_router.composed_lifespan_state = root_state
            root_router.composed_lifespan_active = True

            for application in applications:
                router = application.router
                if router.composed_lifespan_active:
                    continue

                state: dict[str, Any] = {}
                context = router.lifespan_context(application)
                application_state = await context.__aenter__()
                self._entries.append((application, context, state))
                if application_state is not None:
                    state.update(application_state)
                router.composed_lifespan_active = True
                router.composed_lifespan_state = state
        except BaseException as exc:
            await self._shutdown(exc)
            raise

        return maybe_state

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        del exc_type, traceback
        await self._shutdown(exc_value)
        return None

    async def _shutdown(self, initial_exception: BaseException | None = None) -> None:
        exceptions: list[BaseException] = []
        if initial_exception is not None:
            exceptions.append(initial_exception)

        for application, context, _state in reversed(getattr(self, "_entries", [])):
            try:
                await context.__aexit__(
                    type(initial_exception) if initial_exception is not None else None,
                    initial_exception,
                    initial_exception.__traceback__ if initial_exception is not None else None,
                )
            except BaseException as exc:
                if exc is not initial_exception:
                    exceptions.append(exc)
            finally:
                router = getattr(application, "router", None)
                if router is not None:
                    router.composed_lifespan_active = False
                    router.composed_lifespan_state = None

        root_router = getattr(self, "_root_router", None)
        if root_router is not None:
            root_router.composed_lifespan_active = False
            root_router.composed_lifespan_state = None

        if len(exceptions) > 1 and BaseExceptionGroup is not None:
            raise BaseExceptionGroup("Multiple errors occurred during lifespan shutdown.", exceptions)
        if len(exceptions) > 1:
            for index in range(len(exceptions) - 1):
                exceptions[index].__context__ = exceptions[index + 1]
        if exceptions:
            raise exceptions[0]


def composed_lifespan(lifespan: Lifespan[Any] | None = None) -> Lifespan[Any]:
    @asynccontextmanager
    async def lifespan_context(app: Any) -> AsyncIterator[Mapping[str, Any] | None]:
        async with ComposedLifespan(app, lifespan) as state:
            yield state

    lifespan_context._starlette_composed_lifespan = True  # type: ignore[attr-defined]
    return cast(Lifespan[Any], lifespan_context)
