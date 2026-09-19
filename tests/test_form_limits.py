from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Any, ClassVar

import anyio
import pytest

from starlette.applications import Starlette
from starlette.datastructures import Headers, UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MaxBodySizeException, MaxFileSizeException, MultiPartParser
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tests.types import TestClientFactory


@asynccontextmanager
async def aclosing(generator: AsyncGenerator[Any, None]) -> AsyncGenerator[AsyncGenerator[Any, None], None]:
    try:
        yield generator
    finally:
        await generator.aclose()


def make_multipart_body(parts: list[tuple[str, str | None, bytes]]) -> bytes:
    body = b""
    for name, filename, content in parts:
        body += b"--B\r\n"
        if filename is None:
            body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            body += content + b"\r\n"
        else:
            body += (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
                + b"Content-Type: application/octet-stream\r\n\r\n"
                + content
                + b"\r\n"
            )
    return body + b"--B--\r\n"


def multipart_headers() -> Headers:
    return Headers({"content-type": "multipart/form-data; boundary=B"})


async def chunked_bytes(body: bytes, chunk_size: int) -> AsyncGenerator[bytes, None]:
    for index in range(0, len(body), chunk_size):
        if index:
            yield b""
        yield body[index : index + chunk_size]


class TrackedSpooledTemporaryFile(SpooledTemporaryFile[bytes]):
    created: ClassVar[list[TrackedSpooledTemporaryFile]] = []
    close_called: bool

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.close_called = False
        self.created.append(self)

    def close(self) -> None:
        self.close_called = True
        super().close()


async def parse_limits(entry: str, body: bytes, **kwargs: Any) -> Any:
    chunk_size = kwargs.pop("chunk_size", 1)
    if entry == "parser":
        generator = chunked_bytes(body, chunk_size)
        try:
            return await MultiPartParser(multipart_headers(), generator, **kwargs).parse()
        finally:
            await generator.aclose()

    chunks = [body[index : index + chunk_size] for index in range(0, len(body), chunk_size)]
    index = 0

    async def receive() -> Message:
        nonlocal index
        if index < len(chunks):
            chunk = chunks[index]
            index += 1
            return {"type": "http.request", "body": chunk, "more_body": True}
        return {"type": "http.request", "body": b"", "more_body": False}

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", b"multipart/form-data; boundary=B")],
    }
    return await Request(scope, receive).form(**kwargs)


@pytest.fixture
def tracked_files(monkeypatch: pytest.MonkeyPatch) -> list[TrackedSpooledTemporaryFile]:
    TrackedSpooledTemporaryFile.created.clear()
    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", TrackedSpooledTemporaryFile)
    return TrackedSpooledTemporaryFile.created


def make_limit_app(**kwargs: Any) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        await request.form(**kwargs)
        response = JSONResponse({"ok": True})
        await response(scope, receive, send)

    return Starlette(routes=[Mount("/", app=app)])


@pytest.mark.anyio
@pytest.mark.parametrize("entry", ["parser", "request"])
async def test_max_body_counts_multiple_files_headers_and_boundaries(entry: str) -> None:
    body = make_multipart_body(
        [
            ("file", "first.txt", b"ab"),
            ("file", "second.txt", b"cde"),
        ]
    )

    file_content_size = len(b"ab") + len(b"cde")
    with pytest.raises(MaxBodySizeException, match="Body exceeded maximum size"):
        await parse_limits(entry, body, max_body_size=file_content_size, chunk_size=3)

    form = await parse_limits(entry, body, max_body_size=len(body), chunk_size=3)
    files = [value for _, value in form.multi_items() if isinstance(value, UploadFile)]
    assert [await value.read() for value in files] == [b"ab", b"cde"]
    await form.close()


@pytest.mark.anyio
@pytest.mark.parametrize("entry", ["parser", "request"])
async def test_max_file_size_applies_to_each_repeated_file(entry: str) -> None:
    body = make_multipart_body(
        [
            ("file", "small.txt", b"ab"),
            ("file", "large.txt", b"cde"),
        ]
    )

    with pytest.raises(MaxFileSizeException, match="File exceeded maximum size"):
        await parse_limits(entry, body, max_file_size=2, chunk_size=2)

    form = await parse_limits(entry, body, max_file_size=3, chunk_size=2)
    files = [value for _, value in form.multi_items() if isinstance(value, UploadFile)]
    assert [value.filename for value in files] == ["small.txt", "large.txt"]
    assert [await value.read() for value in files] == [b"ab", b"cde"]
    await form.close()


@pytest.mark.anyio
@pytest.mark.parametrize("content_length", [None, "1", "999999"])
async def test_body_limit_ignores_missing_and_forged_content_length(content_length: str | None) -> None:
    body = make_multipart_body([("file", "file.txt", b"abcd")])
    raw_headers = [(b"content-type", b"multipart/form-data; boundary=B")]
    if content_length is not None:
        raw_headers.append((b"content-length", content_length.encode()))

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": raw_headers,
        "app": object(),
    }
    chunks = [body[index : index + 2] for index in range(0, len(body), 2)]
    index = 0

    async def receive() -> Message:
        nonlocal index
        if index < len(chunks):
            chunk = chunks[index]
            index += 1
            return {"type": "http.request", "body": chunk, "more_body": True}
        return {"type": "http.request", "body": b"", "more_body": False}

    with pytest.raises(HTTPException) as exc_info:
        await Request(scope, receive).form(max_body_size=len(body) - 1)

    assert exc_info.value.status_code == 413


@pytest.mark.parametrize("limit_name", ["max_body_size", "max_file_size"])
def test_request_form_returns_413(limit_name: str, test_client_factory: TestClientFactory) -> None:
    body = make_multipart_body([("file", "file.txt", b"abc")])
    client = test_client_factory(make_limit_app(**{limit_name: 1}))
    response = client.post(
        "/",
        content=body,
        headers={"Content-Type": "multipart/form-data; boundary=B", "Transfer-Encoding": "chunked"},
    )
    assert response.status_code == 413


@pytest.mark.anyio
async def test_file_limit_stops_streaming() -> None:
    body = make_multipart_body([("file", "file.txt", b"")]).removesuffix(b"--B--\r\n")
    chunks_read = 0

    async def stream() -> AsyncGenerator[bytes, None]:
        nonlocal chunks_read
        yield body
        chunks_read += 1
        while True:
            yield b"x"
            chunks_read += 1

    async with aclosing(stream()) as request_stream:
        with pytest.raises(MaxFileSizeException):
            await MultiPartParser(multipart_headers(), request_stream, max_file_size=4).parse()

    assert chunks_read <= 5


@pytest.mark.anyio
async def test_limit_error_closes_disk_temporary_files(
    tracked_files: list[TrackedSpooledTemporaryFile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MultiPartParser, "spool_max_size", 1)
    body = make_multipart_body([("file", "small.txt", b"ab"), ("file", "large.txt", b"cdef")])

    with pytest.raises(MaxFileSizeException):
        await parse_limits("parser", body, max_file_size=3, chunk_size=2)

    assert len(tracked_files) == 2
    assert all(getattr(file, "_rolled", False) for file in tracked_files)
    assert all(file.close_called for file in tracked_files)
    assert all(not Path(str(file.name)).exists() for file in tracked_files)


@pytest.mark.anyio
async def test_formdata_close_closes_disk_temporary_files(
    tracked_files: list[TrackedSpooledTemporaryFile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MultiPartParser, "spool_max_size", 1)
    form = await parse_limits(
        "parser",
        make_multipart_body([("file", "file.txt", b"abc")]),
        chunk_size=2,
    )

    assert len(tracked_files) == 1
    assert getattr(tracked_files[0], "_rolled", False)
    assert not tracked_files[0].close_called
    await form.close()
    assert tracked_files[0].close_called
    assert not Path(str(tracked_files[0].name)).exists()


@pytest.mark.anyio
async def test_stream_error_closes_temporary_files(tracked_files: list[TrackedSpooledTemporaryFile]) -> None:
    class StreamReadError(Exception):
        pass

    body = make_multipart_body([("file", "file.txt", b"abc")])[:-10]

    async def stream() -> AsyncGenerator[bytes, None]:
        yield body
        raise StreamReadError

    async with aclosing(stream()) as request_stream:
        with pytest.raises(StreamReadError):
            await MultiPartParser(multipart_headers(), request_stream).parse()

    assert len(tracked_files) == 1
    assert tracked_files[0].close_called


@pytest.mark.anyio
async def test_parse_cancellation_closes_temporary_files(
    tracked_files: list[TrackedSpooledTemporaryFile],
) -> None:
    started = anyio.Event()
    release_stream = anyio.Event()
    body = make_multipart_body([("file", "file.txt", b"abc")])[:-10]

    async def stream() -> AsyncGenerator[bytes, None]:
        yield body
        started.set()
        await release_stream.wait()

    async with aclosing(stream()) as request_stream:
        parser = MultiPartParser(multipart_headers(), request_stream)
        parse_exception: BaseException | None = None

        async def parse() -> None:
            nonlocal parse_exception
            try:
                await parser.parse()
            except BaseException as exc:
                parse_exception = exc
                raise

        with anyio.move_on_after(1) as cancel_scope:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(parse)
                await started.wait()
                cancel_scope.cancel()
                release_stream.set()
                await anyio.sleep_forever()

    assert isinstance(parse_exception, anyio.get_cancelled_exc_class())
    assert len(tracked_files) == 1
    assert tracked_files[0].close_called


@pytest.mark.anyio
async def test_client_disconnect_closes_temporary_files(
    tracked_files: list[TrackedSpooledTemporaryFile],
) -> None:
    body = make_multipart_body([("file", "file.txt", b"abc")])[:-10]
    sent_body = False

    async def receive() -> Message:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": True}
        return {"type": "http.disconnect"}

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", b"multipart/form-data; boundary=B")],
    }

    with pytest.raises(ClientDisconnect):
        await Request(scope, receive).form()

    assert len(tracked_files) == 1
    assert tracked_files[0].close_called
