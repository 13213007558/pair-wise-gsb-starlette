
Starlette includes a `WebSocket` class that fulfils a similar role
to the HTTP request, but that allows sending and receiving data on a websocket.

### WebSocket

Signature: `WebSocket(scope, receive, send)`

```python
from starlette.websockets import WebSocket


async def app(scope, receive, send):
    websocket = WebSocket(scope=scope, receive=receive, send=send)
    await websocket.accept()
    await websocket.send_text('Hello, world!')
    await websocket.close()
```

WebSockets present a mapping interface, so you can use them in the same
way as a `scope`.

For instance: `websocket['path']` will return the ASGI path.

#### URL

The websocket URL is accessed as `websocket.url`.

The property is actually a subclass of `str`, and also exposes all the
components that can be parsed out of the URL.

For example: `websocket.url.path`, `websocket.url.port`, `websocket.url.scheme`.

#### Headers

Headers are exposed as an immutable, case-insensitive, multi-dict.

For example: `websocket.headers['sec-websocket-version']`

#### Query Parameters

Query parameters are exposed as an immutable multi-dict.

For example: `websocket.query_params['search']`

#### Path Parameters

Router path parameters are exposed as a dictionary interface.

For example: `websocket.path_params['username']`

### Accepting the connection

* `await websocket.accept(subprotocol=None, headers=None)`

### Sending data

* `await websocket.send_text(data)`
* `await websocket.send_bytes(data)`
* `await websocket.send_json(data)`

JSON messages default to being sent over text data frames, from version 0.10.0 onwards.
Use `websocket.send_json(data, mode="binary")` to send JSON over binary data frames.

### Receiving data

* `await websocket.receive_text()`
* `await websocket.receive_bytes()`
* `await websocket.receive_json()`

May raise `starlette.websockets.WebSocketDisconnect()`.

JSON messages default to being received over text data frames, from version 0.10.0 onwards.
Use `websocket.receive_json(mode="binary")` to receive JSON over binary data frames.

### Iterating data

* `websocket.iter_text()`
* `websocket.iter_bytes()`
* `websocket.iter_json()`

Similar to `receive_text`, `receive_bytes`, and `receive_json` but returns an
async iterator.

```python hl_lines="7-8"
from starlette.websockets import WebSocket


async def app(scope, receive, send):
    websocket = WebSocket(scope=scope, receive=receive, send=send)
    await websocket.accept()
    async for message in websocket.iter_text():
        await websocket.send_text(f"Message text was: {message}")
    await websocket.close()
```

When `starlette.websockets.WebSocketDisconnect` is raised, the iterator will exit.

### Closing the connection

* `await websocket.close(code=1000, reason=None)`

### Authorization leases

A WebSocket may stay open for hours, but the authorization established during
the handshake should not be trusted for the whole lifetime of that connection.
An authorization lease bounds that trust: the application supplies an absolute
expiration time and may subscribe to asynchronous revocation notifications.
Enter the `websocket.lease()` context before calling `accept()` and run the
whole session inside it.

```python
from starlette.applications import Starlette
from starlette.leases import AuthorizationLease
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket


async def websocket_endpoint(websocket: WebSocket) -> None:
    # Expressed in the monotonic clock used by the lease (anyio's clock here).
    lease = AuthorizationLease(expires_at=3600.0)
    async with websocket.lease(lease):
        await websocket.accept()
        async for message in websocket.iter_text():
            await websocket.send_text(f"Received: {message}")


app = Starlette(routes=[WebSocketRoute("/ws", websocket_endpoint)])
```

While the lease is valid the socket behaves exactly as usual. When the lease
is invalidated — because it expired, a revocation event arrived, or the
notification source failed under the fail-closed policy — Starlette sends
exactly one close frame (code `1008` by default), stops handing new client
messages to the application, rejects any new send with
`AuthorizationLeaseExpired`, and cancels blocked `receive()`/`send()` waits so
the context unwinds. A blocked receive surfaces the invalidation as
`WebSocketDisconnect`, so existing disconnect handling keeps working.

Business work that has already started is not rolled back: cancellation
releases a blocked await, while synchronous work already running on the task
runs to completion. Design handlers so each action reaches a clear boundary on
its own, rather than assuming the framework can undo it.

#### Subscribing to revocation events

The optional `subscribe_revocation` callable receives the guarded `WebSocket`
and returns an async context manager that yields an async generator of
`RevocationEvent`. Because it receives the connection, the application can key
the subscription on the connection's identity (for example a user id stored in
the scope) without introducing any message broker.

```python
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from starlette.leases import AuthorizationLease, RevocationEvent
from starlette.websockets import WebSocket, WebSocketDisconnect


@asynccontextmanager
async def subscribe_revocation(
    websocket: WebSocket,
) -> AsyncGenerator[AsyncGenerator[RevocationEvent, None], None]:
    user_id = websocket.scope["user_id"]

    async def events() -> AsyncGenerator[RevocationEvent, None]:
        # Await whatever in-process signal means this user's access was revoked;
        # no message middleware is required.
        await revocation_registry.wait_for(user_id)
        yield RevocationEvent()

    yield events()


async def websocket_endpoint(websocket: WebSocket) -> None:
    lease = AuthorizationLease(
        expires_at=3600.0,
        subscribe_revocation=subscribe_revocation,
    )
    async with websocket.lease(lease):
        await websocket.accept()
        try:
            async for message in websocket.iter_text():
                await websocket.send_text(f"Received: {message}")
        except WebSocketDisconnect:
            pass
```

#### Notification source failures

A revocation source can raise. `error_policy` decides what happens next rather
than silently treating a broken source as permanent authorization. With
`LeaseErrorPolicy.FAIL_CLOSED` (the default) the lease is invalidated
immediately; with `LeaseErrorPolicy.FAIL_OPEN` the failure is logged and the
lease keeps relying on its expiration deadline.

```python
from starlette.leases import AuthorizationLease, LeaseErrorPolicy

lease = AuthorizationLease(
    expires_at=3600.0,
    subscribe_revocation=subscribe_revocation,
    error_policy=LeaseErrorPolicy.FAIL_OPEN,
)
```

A lease must always provide at least one bound, so constructing one without
either `expires_at` or `subscribe_revocation` raises `ValueError`. The close
code and reason can be customized through `close_code` and `close_reason`, and
a custom implementation of the `MonotonicClock` protocol can be supplied with
`clock` (for example to make expiration deterministic in tests).

### Sending and receiving messages

If you need to send or receive raw ASGI messages then you should use
`websocket.send()` and `websocket.receive()` rather than using the raw `send` and
`receive` callables. This will ensure that the websocket's state is kept
correctly updated.

* `await websocket.send(message)`
* `await websocket.receive()`

### Send Denial Response

If you call `websocket.close()` before calling `websocket.accept()` then
the server will automatically send a HTTP 403 error to the client.

If you want to send a different error response, you can use the
`websocket.send_denial_response()` method. This will send the response
and then close the connection.

* `await websocket.send_denial_response(response)`

This requires the ASGI server to support the WebSocket Denial Response
extension. If it is not supported a `RuntimeError` will be raised.

In the context of `Starlette`, you can also use the `HTTPException` to achieve the same result.

```python
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket


def is_authorized(subprotocols: list[str]):
    if len(subprotocols) != 2:
        return False
    if subprotocols[0] != "Authorization":
        return False
    # Here we are hard coding the token, in a real application you would validate the token
    # against a database or an external service.
    if subprotocols[1] != "token":
        return False
    return True


async def websocket_endpoint(websocket: WebSocket):
    subprotocols = websocket.scope["subprotocols"]
    if not is_authorized(subprotocols):
        raise HTTPException(status_code=401, detail="Unauthorized")
    await websocket.accept("Authorization")
    await websocket.send_text("Hello, world!")
    await websocket.close()


app = Starlette(debug=True, routes=[WebSocketRoute("/ws", websocket_endpoint)])
```

<!-- Test the above with `npx wscat -c ws://localhost:8000/ws -s Authorization -s token` -->
