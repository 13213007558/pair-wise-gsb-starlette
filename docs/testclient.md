
??? abstract "API Reference"
    ::: starlette.testclient.TestClient
        options:
            parameter_headings: false
            show_bases: false
            show_root_heading: true
            heading_level: 3
            filters:
                - "__init__"

The test client allows you to make requests against your ASGI application,
using the `httpx` library.

```python
from starlette.responses import HTMLResponse
from starlette.testclient import TestClient


async def app(scope, receive, send):
    assert scope['type'] == 'http'
    response = HTMLResponse('<html><body>Hello, world!</body></html>')
    await response(scope, receive, send)


def test_app():
    client = TestClient(app)
    response = client.get('/')
    assert response.status_code == 200
```

The test client exposes the same interface as any other `httpx` session.
In particular, note that the calls to make a request are just standard
function calls, not awaitables.

You can use any of `httpx` standard API, such as authentication, session
cookies handling, or file uploads.

For example, to set headers on the TestClient you can do:

```python
client = TestClient(app)

# Set headers on the client for future requests
client.headers = {"Authorization": "..."}
response = client.get("/")

# Set headers for each request separately
response = client.get("/", headers={"Authorization": "..."})
```

And for example to send files with the TestClient:

```python
client = TestClient(app)

# Send a single file
with open("example.txt", "rb") as f:
    response = client.post("/form", files={"file": f})

# Send multiple files
with open("example.txt", "rb") as f1:
    with open("example.png", "rb") as f2:
        files = {"file1": f1, "file2": ("filename", f2, "image/png")}
        response = client.post("/form", files=files)
```

For more information you can check the `httpx` [documentation](https://www.python-httpx.org/advanced/).

By default the `TestClient` will raise any exceptions that occur in the
application. Occasionally you might want to test the content of 500 error
responses, rather than allowing client to raise the server exception. In this
case you should use `client = TestClient(app, raise_server_exceptions=False)`.

!!! note

    If you want the `TestClient` to run the `lifespan` handler,
    you will need to use the `TestClient` as a context manager. It will
    not be triggered when the `TestClient` is instantiated. You can learn more about it
    [here](lifespan.md#running-lifespan-in-tests).

### Change client address

By default, the TestClient will set the client host to `"testserver"` and the port to `50000`.

You can change the client address by setting the `client` attribute of the `TestClient` instance:

```python
client = TestClient(app, client=('localhost', 8000))
```

### Selecting the Async backend

`TestClient` takes arguments `backend` (a string) and `backend_options` (a dictionary).
These options are passed to `anyio.start_blocking_portal()`.
See the [anyio documentation](https://anyio.readthedocs.io/en/stable/basics.html#backend-options)
for more information about the accepted backend options.
By default, `asyncio` is used with default options.

To run `Trio`, pass `backend="trio"`. For example:

```python
def test_app()
    with TestClient(app, backend="trio") as client:
       ...
```

To run `asyncio` with `uvloop`, pass `backend_options={"use_uvloop": True}`.  For example:

```python
def test_app()
    with TestClient(app, backend_options={"use_uvloop": True}) as client:
       ...
```

### Timing out application calls

If an endpoint hangs, calls made with the `TestClient` wait forever by default.
You can bound how long a single application call may run with `app_call_timeout`
(in seconds):

```python
client = TestClient(app, app_call_timeout=5.0)
```

The default can be overridden per call through the `app_call_timeout` request
extension. This works for both HTTP requests and WebSocket handshakes:

```python
response = client.get("/", extensions={"app_call_timeout": 1.0})

with client.websocket_connect("/ws", extensions={"app_call_timeout": 1.0}) as websocket:
    ...
```

Passing `extensions={"app_call_timeout": None}` disables the timeout for that call.

When the deadline expires, `starlette.testclient.AppCallTimeoutError` (a
`TimeoutError` subclass) is raised to the caller, and the application work
created by that call is cancelled. The deadline is scoped to the individual
call: the shared portal, the lifespan task and any other concurrent calls on
the same client keep running. Exceptions raised by the application itself are
never disguised as timeouts, and `AppCallTimeoutError` is raised even when
`raise_server_exceptions=False`.

For WebSockets, the deadline only covers the handshake, up to the connection
being accepted or rejected. Once connected, the session is unbounded again.

!!! note "Synchronous code in worker threads"
    Python cannot force-kill a thread. If the application is executing
    synchronous code in a worker thread when the deadline expires (for
    example a sync endpoint), that thread keeps running in the background
    until the function returns. The guaranteed boundaries are:

    * The caller gets `AppCallTimeoutError` when the deadline expires.
    * The asynchronous side of the call is cancelled, and other concurrent
      calls on the same client are unaffected.
    * If the client is used without the context manager, the per-call portal
      is released in the background; its resources are freed once the
      abandoned worker thread returns.

### Testing WebSocket sessions

You can also test websocket sessions with the test client.

The `httpx` library will be used to build the initial handshake, meaning you
can use the same authentication options and other headers between both http and
websocket testing.

```python
from starlette.testclient import TestClient
from starlette.websockets import WebSocket


async def app(scope, receive, send):
    assert scope['type'] == 'websocket'
    websocket = WebSocket(scope, receive=receive, send=send)
    await websocket.accept()
    await websocket.send_text('Hello, world!')
    await websocket.close()


def test_app():
    client = TestClient(app)
    with client.websocket_connect('/') as websocket:
        data = websocket.receive_text()
        assert data == 'Hello, world!'
```

The operations on session are standard function calls, not awaitables.

It's important to use the session within a context-managed `with` block. This
ensure that the background thread on which the ASGI application is properly
terminated, and that any exceptions that occur within the application are
always raised by the test client.

#### Establishing a test session

* `.websocket_connect(url, subprotocols=None, **options)` - Takes the same set of arguments as `httpx.get()`.

May raise `starlette.websockets.WebSocketDisconnect` if the application does not accept the websocket connection.

`websocket_connect()` must be used as a context manager (in a `with` block).

!!! note
    The `params` argument is not supported by `websocket_connect`. If you need to pass query arguments, hard code it
    directly in the URL.

    ```python
    with client.websocket_connect('/path?foo=bar') as websocket:
        ...
    ```

#### Sending data

* `.send_text(data)` - Send the given text to the application.
* `.send_bytes(data)` - Send the given bytes to the application.
* `.send_json(data, mode="text")` - Send the given data to the application. Use `mode="binary"` to send JSON over binary data frames.

#### Receiving data

* `.receive_text()` - Wait for incoming text sent by the application and return it.
* `.receive_bytes()` - Wait for incoming bytestring sent by the application and return it.
* `.receive_json(mode="text")` - Wait for incoming json data sent by the application and return it. Use `mode="binary"` to receive JSON over binary data frames.

May raise `starlette.websockets.WebSocketDisconnect`.

#### Closing the connection

* `.close(code=1000)` - Perform a client-side close of the websocket connection.

### Asynchronous tests

Sometimes you will want to do async things outside of your application.
For example, you might want to check the state of your database after calling your app
using your existing async database client/infrastructure.

For these situations, using `TestClient` is difficult because it creates it's own event loop and async
resources (like a database connection) often cannot be shared across event loops.
The simplest way to work around this is to just make your entire test async and use an async client, like [httpx.AsyncClient].

Here is an example of such a test:

```python
from httpx import AsyncClient, ASGITransport
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import PlainTextResponse


def hello(request: Request) -> PlainTextResponse:
    return PlainTextResponse("Hello World!")


app = Starlette(routes=[Route("/", hello)])


# if you're using pytest, you'll need to to add an async marker like:
# @pytest.mark.anyio  # using https://github.com/agronholm/anyio
# or install and configure pytest-asyncio (https://github.com/pytest-dev/pytest-asyncio)
async def test_app() -> None:
    # note: you _must_ set `base_url` for relative urls like "/" to work
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/")
        assert r.status_code == 200
        assert r.text == "Hello World!"
```

[httpx.AsyncClient]: https://www.python-httpx.org/advanced/#calling-into-python-web-apps
