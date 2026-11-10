from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from starlette import status
from starlette.leases import (
    AuthorizationLease,
    LeaseErrorPolicy,
    RevocationEvent,
    RuntimeClock,
    _LeaseGuard,
)
from starlette.types import ASGIApp, Message, Scope
from starlette.websockets import (
    AuthorizationLeaseExpired,
    WebSocket,
    WebSocketDisconnect,
)
from tests.types import TestClientFactory


class ManualClock:
    """
    Controllable monotonic clock. ``sleep_until`` only completes when the
    clock is advanced past the deadline, so expiry scenarios are deterministic
    without waiting on wall-clock time.
    """

    def __init__(self) -> None:
        self._now = 0.0
        self._condition = anyio.Condition()

    def now(self) -> float:
        return self._now

    async def sleep_until(self, deadline: float) -> None:
        async with self._condition:
            while self._now < deadline:
                await self._condition.wait()

    async def advance(self, seconds: float) -> None:
        async with self._condition:
            self._now += seconds
            self._condition.notify_all()


class MemoryASGIChannel:
    """
    In-memory ASGI websocket transport backed by two memory streams. The test
    drives the client side while the application coroutine consumes and
    produces ASGI messages on the server side.
    """

    def __init__(self) -> None:
        client_tx: MemoryObjectSendStream[Message]
        client_rx: MemoryObjectReceiveStream[Message]
        server_tx: MemoryObjectSendStream[Message]
        server_rx: MemoryObjectReceiveStream[Message]
        client_tx, server_rx = anyio.create_memory_object_stream(float("inf"))
        server_tx, client_rx = anyio.create_memory_object_stream(float("inf"))
        self._client_tx = client_tx
        self._client_rx = client_rx
        self._server_tx = server_tx
        self._server_rx = server_rx
        self.server_send = server_tx.send
        self.server_receive = server_rx.receive
        self.sent_messages: list[Message] = []

    async def __aenter__(self) -> MemoryASGIChannel:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        for stream in (self._client_tx, self._client_rx, self._server_tx, self._server_rx):
            await stream.aclose()

    async def connect(self) -> None:
        await self._client_tx.send({"type": "websocket.connect"})

    async def send_text(self, text: str) -> None:
        await self._client_tx.send({"type": "websocket.receive", "text": text})

    async def disconnect(self, code: int = status.WS_1000_NORMAL_CLOSURE) -> None:
        await self._client_tx.send({"type": "websocket.disconnect", "code": code, "reason": ""})

    async def break_server_receive(self) -> None:
        # Simulate the transport disappearing without a disconnect message.
        await self._server_rx.aclose()

    async def receive_server_message(self) -> Message:
        message = await self._client_rx.receive()
        self.sent_messages.append(message)
        return message

    async def accept(self) -> None:
        message = await self.receive_server_message()
        assert message["type"] == "websocket.accept"

    async def expect_close(self) -> Message:
        message = await self.receive_server_message()
        assert message["type"] == "websocket.close"
        return message

    def close_frame_count(self) -> int:
        return sum(message["type"] == "websocket.close" for message in self.sent_messages)


def make_scope() -> Scope:
    return {"type": "websocket", "path": "/", "headers": [], "subprotocols": []}


def capture_guards(monkeypatch: pytest.MonkeyPatch) -> list[_LeaseGuard]:
    guards: list[_LeaseGuard] = []
    original_init = _LeaseGuard.__init__

    def record_init(self: _LeaseGuard, websocket: WebSocket[Any], lease: AuthorizationLease) -> None:
        original_init(self, websocket, lease)
        guards.append(self)

    monkeypatch.setattr(_LeaseGuard, "__init__", record_init)
    return guards


async def run_app(app: ASGIApp, channel: MemoryASGIChannel) -> None:
    await app(make_scope(), channel.server_receive, channel.server_send)


Subscriber = Callable[[WebSocket[Any]], Any]


def make_revocation_subscriber(trigger: anyio.Event, *, fail: bool = False) -> Subscriber:
    @asynccontextmanager
    async def subscribe(websocket: WebSocket[Any]) -> AsyncGenerator[AsyncGenerator[RevocationEvent, None], None]:
        # The subscription is keyed on the connection's identity.
        assert websocket.scope["path"] == "/"
        if fail:

            async def failing_events() -> AsyncGenerator[RevocationEvent, None]:
                raise RuntimeError("notification source unavailable")
                yield  # pragma: no cover - makes this an async generator

            yield failing_events()
            return

        async def events() -> AsyncGenerator[RevocationEvent, None]:
            await trigger.wait()
            yield RevocationEvent()

        yield events()

    return subscribe


@pytest.mark.anyio
async def test_lease_valid_for_full_session_allows_normal_traffic() -> None:
    clock = ManualClock()
    delivered: list[str] = []

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(expires_at=100.0, clock=clock)
        async with websocket.lease(lease):
            await websocket.accept()
            async for text in websocket.iter_text():
                delivered.append(text)
                await websocket.send_text(f"echo:{text}")

    async with MemoryASGIChannel() as channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        await channel.send_text("one")
        await channel.send_text("two")
        assert (await channel.receive_server_message())["text"] == "echo:one"
        assert (await channel.receive_server_message())["text"] == "echo:two"
        await clock.advance(50)
        await anyio.sleep(0)
        await channel.disconnect()

    assert delivered == ["one", "two"]


@pytest.mark.anyio
async def test_expiry_stops_delivery_and_sends_single_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    delivered: list[str] = []
    guards = capture_guards(monkeypatch)

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(expires_at=10.0, clock=clock)
        async with websocket.lease(lease):
            await websocket.accept()
            async for text in websocket.iter_text():
                delivered.append(text)

    async with MemoryASGIChannel() as channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        await channel.send_text("before")
        await anyio.sleep(0.02)
        await clock.advance(10)
        close_message = await channel.expect_close()
        # Further expiry signals and late client messages change nothing.
        await clock.advance(10)
        await channel.send_text("after")
        await anyio.sleep(0.02)
        await channel.disconnect()

    assert close_message["code"] == status.WS_1008_POLICY_VIOLATION
    assert close_message["reason"] == "Authorization lease expired"
    assert delivered == ["before"]
    assert channel.close_frame_count() == 1
    assert guards[0].invalidated_by == "expired"


@pytest.mark.anyio
async def test_revocation_event_invalidates_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    revoke = anyio.Event()
    accepted = anyio.Event()
    delivered: list[str] = []
    guards = capture_guards(monkeypatch)

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(
            expires_at=100.0,
            subscribe_revocation=make_revocation_subscriber(revoke),
            clock=clock,
        )
        async with websocket.lease(lease):
            await websocket.accept()
            accepted.set()
            try:
                async for text in websocket.iter_text():
                    delivered.append(text)
            except WebSocketDisconnect:
                pass

    async with MemoryASGIChannel() as channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        await accepted.wait()
        await channel.send_text("allowed")
        await anyio.sleep(0.02)
        revoke.set()
        close_message = await channel.expect_close()
        await channel.send_text("blocked")
        await anyio.sleep(0.02)
        await channel.disconnect()

    assert close_message["code"] == status.WS_1008_POLICY_VIOLATION
    assert delivered == ["allowed"]
    assert channel.close_frame_count() == 1
    assert guards[0].invalidated_by == "revoked"


@pytest.mark.anyio
async def test_blocked_receive_unwinds_as_disconnect_on_expiry() -> None:
    clock = ManualClock()
    observed: dict[str, Any] = {}

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(expires_at=10.0, clock=clock)
        async with websocket.lease(lease):
            await websocket.accept()
            try:
                await websocket.receive_text()
            except WebSocketDisconnect as exc:
                observed["code"] = exc.code
                observed["reason"] = exc.reason

    async with MemoryASGIChannel() as channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        await anyio.sleep(0.02)
        await clock.advance(10)
        close_message = await channel.expect_close()

    assert observed == {"code": close_message["code"], "reason": close_message["reason"]}


@pytest.mark.anyio
async def test_send_after_invalidation_is_rejected() -> None:
    clock = ManualClock()
    observed: dict[str, Any] = {}
    allow_send = anyio.Event()
    send_started = anyio.Event()

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(expires_at=10.0, clock=clock)
        async with websocket.lease(lease):
            await websocket.accept()
            # Shield the send attempt from the lease's cancellation so the only
            # thing that stops it is the guard's send rejection.
            with anyio.CancelScope(shield=True):
                send_started.set()
                await allow_send.wait()
                try:
                    await websocket.send_text("too late")
                except AuthorizationLeaseExpired as exc:
                    observed["code"] = exc.code
                    observed["reason"] = exc.reason

    async with MemoryASGIChannel() as channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        await send_started.wait()
        await clock.advance(10)
        await channel.expect_close()
        allow_send.set()
        await anyio.sleep(0.02)

    assert observed == {"code": status.WS_1008_POLICY_VIOLATION, "reason": "Authorization lease expired"}
    assert channel.close_frame_count() == 1
    # No data frame may escape after the close frame.
    assert not any(message["type"] == "websocket.send" for message in channel.sent_messages)


@pytest.mark.anyio
async def test_client_disconnect_does_not_send_close_frame() -> None:
    clock = ManualClock()
    delivered: list[str] = []

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(expires_at=100.0, clock=clock)
        async with websocket.lease(lease):
            await websocket.accept()
            async for text in websocket.iter_text():
                delivered.append(text)

    async with MemoryASGIChannel() as channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        await channel.send_text("hi")
        await anyio.sleep(0.02)
        await channel.disconnect(code=status.WS_1001_GOING_AWAY)
        await anyio.sleep(0.02)
        # Expiry arriving after the client disconnect must not emit a close.
        await clock.advance(100)
        await anyio.sleep(0.02)

    assert delivered == ["hi"]
    assert channel.close_frame_count() == 0


@pytest.mark.anyio
async def test_concurrent_disconnect_and_expiry_send_one_close() -> None:
    clock = ManualClock()

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(expires_at=10.0, clock=clock)
        async with websocket.lease(lease):
            await websocket.accept()
            try:
                # Block on a receive so expiry cancelling the wait surfaces as
                # a disconnect rather than a raw cancellation.
                await websocket.receive_text()
            except WebSocketDisconnect:
                pass

    async with MemoryASGIChannel() as channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        await clock.advance(10)
        await channel.disconnect()
        close_message = await channel.expect_close()

    assert close_message["code"] == status.WS_1008_POLICY_VIOLATION
    assert channel.close_frame_count() == 1


@pytest.mark.anyio
async def test_notification_error_fail_closed_invalidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    guards = capture_guards(monkeypatch)

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(
            expires_at=100.0,
            subscribe_revocation=make_revocation_subscriber(anyio.Event(), fail=True),
            error_policy=LeaseErrorPolicy.FAIL_CLOSED,
            clock=clock,
        )
        async with websocket.lease(lease):
            try:
                await websocket.accept()
                await websocket.receive_text()
            except WebSocketDisconnect:
                pass

    async with MemoryASGIChannel() as channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        close_message = await channel.expect_close()

    assert close_message["code"] == status.WS_1008_POLICY_VIOLATION
    assert channel.close_frame_count() == 1
    assert guards[0].invalidated_by == "notification_error"


@pytest.mark.anyio
async def test_notification_error_fail_open_keeps_connection_until_expiry(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    guards = capture_guards(monkeypatch)

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(
            expires_at=10.0,
            subscribe_revocation=make_revocation_subscriber(anyio.Event(), fail=True),
            error_policy=LeaseErrorPolicy.FAIL_OPEN,
            clock=clock,
        )
        async with websocket.lease(lease):
            await websocket.accept()
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                pass

    with caplog.at_level("WARNING", logger="starlette.websockets.lease"):
        async with MemoryASGIChannel() as channel, anyio.create_task_group() as task_group:
            task_group.start_soon(run_app, app, channel)
            await channel.connect()
            await channel.accept()
            await anyio.sleep(0.02)
            # The broken notification source alone does not close the socket.
            assert channel.close_frame_count() == 0
            assert guards[0].notify_failed is True
            assert guards[0].invalidated_by is None
            await clock.advance(10)
            close_message = await channel.expect_close()

    assert close_message["code"] == status.WS_1008_POLICY_VIOLATION
    assert channel.close_frame_count() == 1
    assert "Revocation notification source failed" in caplog.text


@pytest.mark.anyio
async def test_inflight_work_completes_but_later_sends_are_blocked() -> None:
    clock = ManualClock()
    delivered: list[str] = []
    revoked = anyio.Event()
    accepted = anyio.Event()
    handler_reached = anyio.Event()
    release_handler = anyio.Event()

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(
            expires_at=100.0,
            subscribe_revocation=make_revocation_subscriber(revoked),
            clock=clock,
        )
        async with websocket.lease(lease):
            try:
                await websocket.accept()
                accepted.set()
                # Shield the in-flight business action from the lease's
                # cancellation so it runs to its own boundary; the framework
                # never rolls it back.
                with anyio.CancelScope(shield=True):
                    message = await websocket.receive_text()
                    delivered.append(message)
                    handler_reached.set()
                    await release_handler.wait()
                    # This send starts after revocation; the guard rejects it
                    # rather than claiming to undo work already completed.
                    try:
                        await websocket.send_text("done")
                    except AuthorizationLeaseExpired:
                        delivered.append("send-blocked")
            except WebSocketDisconnect:
                pass

    async with MemoryASGIChannel() as channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        await accepted.wait()
        await channel.send_text("work")
        await handler_reached.wait()
        revoked.set()
        await channel.expect_close()
        release_handler.set()
        await anyio.sleep(0.02)
        await channel.disconnect()

    assert delivered == ["work", "send-blocked"]
    assert channel.close_frame_count() == 1


@pytest.mark.anyio
async def test_expiry_before_accept_denies_handshake() -> None:
    clock = ManualClock()
    accepted = anyio.Event()

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(expires_at=10.0, clock=clock)
        async with websocket.lease(lease):
            await anyio.sleep(0.02)
            await websocket.accept()
            accepted.set()

    async with MemoryASGIChannel() as channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await clock.advance(10)
        close_message = await channel.expect_close()

    # The handshake never completes because the lease lapsed first.
    assert accepted.is_set() is False
    assert close_message["code"] == status.WS_1008_POLICY_VIOLATION
    assert channel.close_frame_count() == 1


def test_lease_requires_expiration_or_revocation() -> None:
    with pytest.raises(ValueError, match="expiration or a revocation source"):
        AuthorizationLease()


def test_lease_rejects_invalid_close_code() -> None:
    with pytest.raises(ValueError, match="WebSocket close codes"):
        AuthorizationLease(expires_at=1.0, close_code=2999)
    lease = AuthorizationLease(expires_at=1.0, close_code=status.WS_1001_GOING_AWAY)
    assert lease.close_code == status.WS_1001_GOING_AWAY


def test_lease_expired_exception_carries_code_and_reason() -> None:
    exc = AuthorizationLeaseExpired(code=4000, reason="token rotated")
    assert exc.code == 4000
    assert exc.reason == "token rotated"
    assert "4000" in str(exc)


@pytest.mark.anyio
async def test_runtime_clock_drives_default_expiration() -> None:
    # No explicit clock: the guard must fall back to the event-loop clock.
    deadline = RuntimeClock().now() + 0.05
    channel = MemoryASGIChannel()

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(expires_at=deadline)
        async with websocket.lease(lease):
            await websocket.accept()
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                pass

    async with channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        close_message = await channel.expect_close()

    assert close_message["code"] == status.WS_1008_POLICY_VIOLATION
    assert channel.close_frame_count() == 1


@pytest.mark.anyio
async def test_revocation_only_lease_without_expiration() -> None:
    revoke = anyio.Event()
    channel = MemoryASGIChannel()

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(subscribe_revocation=make_revocation_subscriber(revoke))
        async with websocket.lease(lease):
            await websocket.accept()
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                pass

    async with channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        revoke.set()
        close_message = await channel.expect_close()

    assert close_message["code"] == status.WS_1008_POLICY_VIOLATION
    assert channel.close_frame_count() == 1


@pytest.mark.anyio
async def test_receive_after_clean_client_disconnect_raises_disconnected() -> None:
    clock = ManualClock()
    channel = MemoryASGIChannel()
    observed: dict[str, Any] = {}

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(expires_at=100.0, clock=clock)
        async with websocket.lease(lease):
            await websocket.accept()
            # First receive consumes the disconnect; a second one must report a
            # closed socket rather than an invalidated lease.
            first = await websocket.receive()
            assert first["type"] == "websocket.disconnect"
            try:
                await websocket.receive()
            except Exception as exc:
                observed["type"] = type(exc).__name__

    async with channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        await channel.disconnect()

    assert observed == {"type": "WebSocketDisconnected"}


@pytest.mark.anyio
async def test_broken_transport_unblocks_receive_with_disconnected() -> None:
    clock = ManualClock()
    channel = MemoryASGIChannel()
    observed: dict[str, Any] = {}

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(expires_at=100.0, clock=clock)
        async with websocket.lease(lease):
            await websocket.accept()
            try:
                await websocket.receive_text()
            except Exception as exc:
                observed["type"] = type(exc).__name__

    async with channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        await channel.break_server_receive()

    assert observed == {"type": "WebSocketDisconnected"}


@pytest.mark.anyio
async def test_app_close_then_expiry_does_not_emit_second_close() -> None:
    clock = ManualClock()
    channel = MemoryASGIChannel()

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(expires_at=10.0, clock=clock)
        async with websocket.lease(lease):
            await websocket.accept()
            await websocket.close(code=status.WS_1001_GOING_AWAY)
            # Expiry landing after an explicit close must reuse the invalidation
            # guard without sending another close frame.
            await clock.advance(10)

    async with channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        close_message = await channel.expect_close()

    assert close_message["code"] == status.WS_1001_GOING_AWAY
    assert channel.close_frame_count() == 1


@pytest.mark.anyio
async def test_second_invalidation_is_idempotent() -> None:
    clock = ManualClock()
    revoke = anyio.Event()
    channel = MemoryASGIChannel()

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(
            expires_at=10.0,
            subscribe_revocation=make_revocation_subscriber(revoke),
            clock=clock,
        )
        async with websocket.lease(lease):
            await websocket.accept()
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                pass

    async with channel, anyio.create_task_group() as task_group:
        task_group.start_soon(run_app, app, channel)
        await channel.connect()
        await channel.accept()
        revoke.set()
        close_message = await channel.expect_close()
        # Expiry after revocation must not change the single close frame.
        await clock.advance(10)
        await anyio.sleep(0.02)

    assert close_message["code"] == status.WS_1008_POLICY_VIOLATION
    assert channel.close_frame_count() == 1


def test_private_close_code_is_accepted() -> None:
    lease = AuthorizationLease(expires_at=1.0, close_code=4001, close_reason="rotated")
    assert lease.close_code == 4001
    assert lease.close_reason == "rotated"


def test_unleased_connection_keeps_ordinary_usage(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        await websocket.accept()
        await websocket.send_text("ordinary")
        await websocket.close()

    client = test_client_factory(app)
    with client.websocket_connect("/") as session:
        assert session.receive_text() == "ordinary"


def test_lease_expiry_closes_real_testclient_connection(
    test_client_factory: TestClientFactory,
) -> None:
    # The default event-loop clock drives expiry on a short real-time deadline.
    async def app(scope: Scope, receive: Any, send: Any) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        lease = AuthorizationLease(expires_at=RuntimeClock().now() + 0.1)
        async with websocket.lease(lease):
            await websocket.accept()
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                pass

    client = test_client_factory(app)
    with client.websocket_connect("/") as session:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            session.receive_text()
    assert exc_info.value.code == status.WS_1008_POLICY_VIOLATION
