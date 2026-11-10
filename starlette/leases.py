from __future__ import annotations

import enum
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from starlette.types import Message
from starlette.websockets import (
    AuthorizationLeaseExpired,
    WebSocketDisconnect,
    WebSocketDisconnected,
    WebSocketState,
)

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

logger = logging.getLogger("starlette.websockets.lease")

_STREAM_CLOSED_ERRORS = (anyio.ClosedResourceError, anyio.EndOfStream)


class MonotonicClock(Protocol):
    """
    A pluggable source of monotonic time.

    Implementations must expose a single, never-decreasing clock so lease
    deadlines stay unambiguous. ``sleep_until`` must be cancellable, which
    lets the supervisor release the expiry watcher when the connection ends.
    """

    def now(self) -> float: ...

    async def sleep_until(self, deadline: float) -> None: ...


class RuntimeClock:
    """
    Clock backed by the running ``anyio`` event loop.
    """

    def now(self) -> float:
        return anyio.current_time()

    async def sleep_until(self, deadline: float) -> None:
        await anyio.sleep_until(deadline)


class LeaseErrorPolicy(enum.Enum):
    """
    Reaction to a failure of the revocation notification source.

    ``FAIL_CLOSED`` treats a broken notification source as an immediate lease
    invalidation. ``FAIL_OPEN`` logs the failure and keeps relying on the
    expiration deadline instead of silently assuming permanent authorization.
    """

    FAIL_CLOSED = "fail_closed"
    FAIL_OPEN = "fail_open"


class RevocationEvent:
    """
    Marker yielded by a revocation stream when access must be revoked.

    Applications may subclass it to attach context about why the lease was
    revoked; the supervisor only relies on an event being yielded.
    """


RevocationStream = AsyncGenerator[RevocationEvent, None]
RevocationSubscriber = Callable[["WebSocket[Any]"], AbstractAsyncContextManager[RevocationStream]]


@dataclass(frozen=True, slots=True)
class AuthorizationLease:
    """
    Time-bounded authorization granted while accepting a WebSocket.

    ``expires_at`` is an absolute deadline expressed in the configured clock's
    units, and ``subscribe_revocation`` optionally yields per-connection
    revocation events. A failing notification source is handled according to
    ``error_policy`` rather than being ignored as permanent authorization.
    """

    expires_at: float | None = None
    subscribe_revocation: RevocationSubscriber | None = None
    error_policy: LeaseErrorPolicy = LeaseErrorPolicy.FAIL_CLOSED
    close_code: int = 1008
    close_reason: str = "Authorization lease expired"
    clock: MonotonicClock | None = None

    def __post_init__(self) -> None:
        code = self.close_code
        is_registered = 1000 <= code <= 1015
        is_private = 3000 <= code <= 4999
        if not (is_registered or is_private):
            raise ValueError("WebSocket close codes must be in the range 1000-1015 or 3000-4999.")
        if self.expires_at is None and self.subscribe_revocation is None:
            raise ValueError("An authorization lease requires an expiration or a revocation source.")


class _LeaseGuard:
    """
    Supervises one WebSocket connection against an :class:`AuthorizationLease`.

    The guard wraps the connection's ``receive``/``send`` callables and runs
    reader and watcher tasks alongside the application code. Once the lease is
    invalidated, no further client messages reach the application and new
    sends are rejected. Exactly one close frame is sent, the application
    cancel scope releases blocked waits, and all helper tasks are torn down
    when the context exits.
    """

    def __init__(self, websocket: WebSocket[Any], lease: AuthorizationLease) -> None:
        self._websocket = websocket
        self._lease = lease
        self._clock: MonotonicClock = lease.clock or RuntimeClock()
        self._invalidated = False
        self.notify_failed = False
        self.invalidated_by: str | None = None
        self._raw_receive = websocket._receive
        self._raw_send = websocket._send
        message_tx: MemoryObjectSendStream[Message]
        message_rx: MemoryObjectReceiveStream[Message]
        message_tx, message_rx = anyio.create_memory_object_stream(float("inf"))
        self._message_tx = message_tx
        self._message_rx = message_rx
        self._application_scope: anyio.CancelScope | None = None
        self._finished = anyio.Event()

    @asynccontextmanager
    async def _guarded_send(self) -> AsyncIterator[None]:
        websocket = self._websocket

        def guard(message: Message) -> None:
            # Runs before the WebSocket state machine, so a data frame that
            # never started is rejected with a lease-specific error even after
            # the connection was moved to DISCONNECTED.
            if self._invalidated and message["type"] not in {"websocket.close", "websocket.accept"}:
                raise AuthorizationLeaseExpired(code=self._lease.close_code, reason=self._lease.close_reason)

        websocket._lease_send_guard = guard
        try:
            yield
        finally:
            websocket._lease_send_guard = None

    async def _reader(self) -> None:
        message_tx = self._message_tx
        try:
            while True:
                message = await self._raw_receive()
                if self._invalidated:
                    break
                await message_tx.send(message)
                if message["type"] == "websocket.disconnect":
                    break
        except anyio.get_cancelled_exc_class():
            raise
        except _STREAM_CLOSED_ERRORS:
            # The transport vanished without a disconnect message; close the
            # application-facing stream so a blocked ``receive`` wakes up rather
            # than hanging forever.
            await self._message_tx.aclose()

    async def _receive(self) -> Message:
        try:
            return await self._message_rx.receive()
        except _STREAM_CLOSED_ERRORS:
            if self._invalidated:
                raise WebSocketDisconnect(code=self._lease.close_code, reason=self._lease.close_reason) from None
            raise WebSocketDisconnected("The WebSocket connection was closed.") from None

    async def invalidate(self, cause: str) -> None:
        if self._invalidated:
            return
        self._invalidated = True
        self.invalidated_by = cause
        self._message_tx.close()
        if self._application_scope is not None:
            self._application_scope.cancel()
        websocket = self._websocket
        if websocket.application_state in {WebSocketState.CONNECTING, WebSocketState.CONNECTED}:
            await self._raw_send(
                {"type": "websocket.close", "code": self._lease.close_code, "reason": self._lease.close_reason}
            )
            websocket.application_state = WebSocketState.DISCONNECTED

    async def _expiry_watcher(self) -> None:
        deadline = self._lease.expires_at
        if deadline is None:
            await self._finished.wait()
            return
        # Cancelled by ``_stop_watchers_when_finished`` once the application
        # exits, so reaching this point means the deadline genuinely passed.
        await self._clock.sleep_until(deadline)
        await self.invalidate("expired")

    async def _revocation_watcher(self) -> None:
        subscriber = self._lease.subscribe_revocation
        if subscriber is None:
            await self._finished.wait()
            return
        try:
            async with subscriber(self._websocket) as events, aclosing(events) as stream:
                async for _event in stream:
                    await self.invalidate("revoked")
                    break
        except anyio.get_cancelled_exc_class():
            raise
        except Exception:
            if self._lease.error_policy is LeaseErrorPolicy.FAIL_CLOSED:
                await self.invalidate("notification_error")
            else:
                self.notify_failed = True
                logger.warning("Revocation notification source failed; relying on lease expiration.", exc_info=True)

    async def _run_watchers(self, watcher_scope: anyio.CancelScope) -> None:
        # The scope is entered in this dedicated task, so cancelling it releases
        # the watchers without cancelling the application task that owns the
        # ``activate`` context manager.
        with watcher_scope:
            async with anyio.create_task_group() as watcher_group:
                watcher_group.start_soon(self._reader)
                watcher_group.start_soon(self._expiry_watcher)
                watcher_group.start_soon(self._revocation_watcher)

    async def _stop_watchers_when_finished(self, watcher_scope: anyio.CancelScope) -> None:
        await self._finished.wait()
        watcher_scope.cancel()

    @asynccontextmanager
    async def activate(self) -> AsyncIterator[_LeaseGuard]:
        websocket = self._websocket
        watcher_scope = anyio.CancelScope()
        async with (
            anyio.create_task_group() as task_group,
            self._guarded_send(),
            self._message_tx,
            self._message_rx,
        ):
            task_group.start_soon(self._run_watchers, watcher_scope)
            task_group.start_soon(self._stop_watchers_when_finished, watcher_scope)
            websocket._receive = self._receive
            try:
                with anyio.CancelScope() as application_scope:
                    self._application_scope = application_scope
                    yield self
            finally:
                self._application_scope = None
                self._finished.set()
                websocket._receive = self._raw_receive
                application_scope.cancel()


__all__ = [
    "AuthorizationLease",
    "LeaseErrorPolicy",
    "MonotonicClock",
    "RevocationEvent",
    "RevocationStream",
    "RevocationSubscriber",
    "RuntimeClock",
]
