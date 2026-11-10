"""Ownership fault tests for multipart temporary files.

These tests pin down who is responsible for closing the spooled temporary
files created while parsing a ``multipart/form-data`` request.  The rules
under test are:

* While parsing, the :class:`MultiPartParser` owns every file it created.  If
  the request is cancelled at any ``await`` suspension point (reading the
  next stream chunk, a threadpool ``write`` or ``seek``), or if parsing raises
  for any other reason, the parser must close *every* file it created.
* A file whose ``close()`` itself fails must not prevent the remaining files
  from being closed, and the original failure must stay the exception that
  propagates.
* Once :meth:`MultiPartParser.parse` returns a :class:`FormData`, ownership
  moves to the application.  The parser must not perform any defensive close
  on the success path: the application keeps fully usable handles and releases
  them through the normal request/form context.

Determinism is provided by a counting file double and event synchronisation.
No sockets are opened and no fixed sleeps are used to manufacture races.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Iterator
from tempfile import SpooledTemporaryFile
from typing import Any, cast
from unittest import mock

import anyio
import pytest

from starlette.datastructures import FormData, Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request
from starlette.types import Message, Scope

BOUNDARY = "starletteOwnershipBoundary123456"
CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"
# Keep the in-memory spool tiny so a single ordinary part crosses the
# memory-to-disk threshold and its write is routed to a worker thread.
SMALL_SPOOL = 16
WAIT_TIMEOUT = 10


class CountingSpooledFile(SpooledTemporaryFile[bytes]):
    """Spooled file double that counts creation/close and can suspend ops.

    Instances register themselves with the per-test :class:`FileRegistry`
    stored on the class.  A blocking policy may pause a ``write`` or ``seek``
    inside a worker thread until the driver releases the gate, and ``close``
    may be configured to raise.
    """

    registry: FileRegistry | None = None
    block_policy: dict[str, tuple[str, threading.Event, threading.Event]] = {}
    failing_close_labels: set[str] = set()

    def __init__(self, max_size: int | None = None, label: str | None = None) -> None:
        super().__init__(max_size=max_size if max_size is not None else 0)
        registry = CountingSpooledFile.registry
        assert registry is not None
        self.label = label or f"file{len(registry.instances) + 1}"
        registry.instances.append(self)
        registry.created_labels.append(self.label)

    def write(self, s: bytes) -> int:  # type: ignore[override]
        policy = CountingSpooledFile.block_policy.get(self.label)
        if policy is not None and policy[0] == "write":
            _, entered, gate = policy
            entered.set()
            gate.wait(WAIT_TIMEOUT)
        return super().write(s)

    def seek(self, *args: Any, **kwargs: Any) -> Any:
        policy = CountingSpooledFile.block_policy.get(self.label)
        if policy is not None and policy[0] == "seek":
            _, entered, gate = policy
            entered.set()
            gate.wait(WAIT_TIMEOUT)
        return super().seek(*args, **kwargs)

    def close(self) -> None:
        registry = CountingSpooledFile.registry
        assert registry is not None
        registry.close_attempt_labels.append(self.label)
        if self.label in CountingSpooledFile.failing_close_labels:
            raise OSError(f"injected close failure for {self.label}")
        super().close()


class FileRegistry:
    """Per-test record of the counting files and their lifecycle."""

    def __init__(self) -> None:
        self.instances: list[CountingSpooledFile] = []
        self.created_labels: list[str] = []
        self.close_attempt_labels: list[str] = []

    @property
    def created_count(self) -> int:
        return len(self.created_labels)

    @property
    def close_attempt_count(self) -> int:
        return len(self.close_attempt_labels)

    @property
    def closed_count(self) -> int:
        return sum(file.closed for file in self.instances)

    def labels(self) -> list[str]:
        return [file.label for file in self.instances]

    def factory(self, max_size: int | None = None) -> CountingSpooledFile:
        return CountingSpooledFile(max_size=max_size)

    def force_close_remaining(self) -> None:
        """Release files a test deliberately left open so teardown stays clean."""
        for file in self.instances:
            if not file.closed:
                SpooledTemporaryFile.close(file)


class ControlledStream:
    """Async byte stream whose next-chunk suspension point is observable.

    ``chunk_reached`` is set once the parser is parked waiting for the gated
    chunk, letting the driver cancel that exact ``await`` rather than racing
    against it.  ``fail_on_end`` raises instead of terminating normally once
    every chunk has been delivered, simulating a transport failure after the
    parser processed the payload.
    """

    def __init__(
        self,
        chunks: list[bytes],
        *,
        gated_chunk_index: int | None = None,
        chunk_reached: threading.Event | None = None,
        chunk_gate: threading.Event | None = None,
        chunk_resumed: threading.Event | None = None,
        fail_on_end: bool = False,
    ) -> None:
        self._chunks = chunks
        self._gated_chunk_index = gated_chunk_index
        self._chunk_reached = chunk_reached
        self._chunk_gate = chunk_gate
        self._chunk_resumed = chunk_resumed
        self._fail_on_end = fail_on_end
        self._index = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        if self._index >= len(self._chunks):
            if self._fail_on_end:
                raise RuntimeError("injected stream failure")
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        if self._gated_chunk_index is not None and self._index - 1 == self._gated_chunk_index:
            assert self._chunk_reached is not None and self._chunk_gate is not None
            self._chunk_reached.set()
            await anyio.to_thread.run_sync(self._chunk_gate.wait, WAIT_TIMEOUT)
            if self._chunk_resumed is not None:
                self._chunk_resumed.set()
            # Explicit cancellation checkpoint: the worker call completes even
            # when the surrounding scope was already cancelled, so the pending
            # cancellation is delivered here rather than at an unspecified later
            # point in the parser's iteration loop.
            await anyio.lowlevel.checkpoint()
        return chunk


class SmallSpoolParser(MultiPartParser):
    spool_max_size = SMALL_SPOOL


def make_parser(stream: AsyncIterable[bytes]) -> SmallSpoolParser:
    # The parser only needs an async iterable of chunks, although its declared
    # parameter type is the narrower AsyncGenerator.
    return SmallSpoolParser(
        Headers({"Content-Type": CONTENT_TYPE}),
        cast(AsyncGenerator[bytes, None], stream),
    )


def part_header(field_name: str, filename: str) -> bytes:
    return (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()


def file_part(field_name: str, filename: str, data: bytes, *, last: bool = False) -> bytes:
    body = part_header(field_name, filename) + data + b"\r\n"
    if last:
        body += f"--{BOUNDARY}--\r\n".encode()
    return body


@pytest.fixture
def file_registry() -> Iterator[FileRegistry]:
    registry = FileRegistry()
    previous_registry = CountingSpooledFile.registry
    previous_policy = dict(CountingSpooledFile.block_policy)
    previous_failures = set(CountingSpooledFile.failing_close_labels)
    CountingSpooledFile.registry = registry
    CountingSpooledFile.block_policy = {}
    CountingSpooledFile.failing_close_labels = set()
    with mock.patch(
        "starlette.formparsers.SpooledTemporaryFile",
        side_effect=registry.factory,
    ):
        yield registry
    # Tests that inject a failing close intentionally leave a handle open;
    # release it here so garbage collection cannot raise during teardown.
    CountingSpooledFile.failing_close_labels = set()
    CountingSpooledFile.block_policy = {}
    registry.force_close_remaining()
    CountingSpooledFile.registry = previous_registry
    CountingSpooledFile.block_policy = previous_policy
    CountingSpooledFile.failing_close_labels = previous_failures


async def _wait(event: threading.Event) -> None:
    assert await anyio.to_thread.run_sync(event.wait, WAIT_TIMEOUT)


async def _release_gate(gate: threading.Event, resumed: threading.Event | None = None) -> None:
    """Open a worker gate and wait for the blocked task to pass its checkpoint.

    Releasing a ``threading.Event`` does not itself schedule the waiting task.
    When ``resumed`` is supplied, the driver blocks until the task has run past
    its post-gate cancellation checkpoint, so the test never depends on
    scheduler ordering or on fixed sleeps.
    """
    gate.set()
    if resumed is not None:
        await _wait(resumed)
    else:
        await anyio.lowlevel.checkpoint()


def _assert_all_closed(registry: FileRegistry) -> None:
    assert registry.created_count > 0
    assert registry.closed_count == registry.created_count
    assert sorted(registry.close_attempt_labels) == sorted(registry.labels())


@pytest.mark.anyio
async def test_cancel_while_reading_next_chunk_closes_created_files(
    file_registry: FileRegistry,
) -> None:
    """Cancellation parked on the stream read closes the already-created file."""
    chunk_reached = threading.Event()
    chunk_gate = threading.Event()
    chunk_resumed = threading.Event()
    # The gated chunk is only the second part's header: once the parser parks
    # waiting for it, exactly one file (the completed first part) exists.
    chunks = [
        file_part("first", "a.bin", b"x" * 8),
        part_header("second", "b.bin"),
        b"y" * 8 + b"\r\n" + f"--{BOUNDARY}--\r\n".encode(),
    ]
    stream = ControlledStream(
        chunks,
        gated_chunk_index=1,
        chunk_reached=chunk_reached,
        chunk_gate=chunk_gate,
        chunk_resumed=chunk_resumed,
    )
    parser = make_parser(stream)

    captured: dict[str, BaseException] = {}

    async def runner() -> None:
        try:
            await parser.parse()
        except BaseException as exc:
            # Captured so the task group can finish and we can assert the type.
            captured["exc"] = exc

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(runner)
        await _wait(chunk_reached)
        assert file_registry.created_count == 1
        task_group.cancel_scope.cancel()
        await _release_gate(chunk_gate, chunk_resumed)

    assert isinstance(captured.get("exc"), anyio.get_cancelled_exc_class())
    assert file_registry.created_count == 1
    assert file_registry.close_attempt_count == 1
    _assert_all_closed(file_registry)


@pytest.mark.anyio
async def test_cancel_during_threadpool_write_closes_created_files(
    file_registry: FileRegistry,
) -> None:
    """Cancellation requested at a rolled-over write closes the created file.

    A threadpool call cannot be interrupted while it runs, so cancellation is
    requested while the write is parked and delivered at the parser's next
    suspension point (the following stream read).  The cleanup path must still
    close every file created up to that point.
    """
    write_entered, write_gate = threading.Event(), threading.Event()
    read_reached, read_gate, read_resumed = threading.Event(), threading.Event(), threading.Event()
    CountingSpooledFile.block_policy = {"file1": ("write", write_entered, write_gate)}
    chunks = [
        part_header("first", "a.bin") + b"x" * (SMALL_SPOOL * 2),
        b"\r\n" + f"--{BOUNDARY}--\r\n".encode(),
    ]
    stream = ControlledStream(
        chunks,
        gated_chunk_index=1,
        chunk_reached=read_reached,
        chunk_gate=read_gate,
        chunk_resumed=read_resumed,
    )
    parser = make_parser(stream)

    captured: dict[str, BaseException] = {}

    async def runner() -> None:
        try:
            await parser.parse()
        except BaseException as exc:
            # Captured so the task group can finish and we can assert the type.
            captured["exc"] = exc

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(runner)
        await _wait(write_entered)
        assert file_registry.created_count == 1
        task_group.cancel_scope.cancel()
        await _release_gate(write_gate)
        await _wait(read_reached)
        await _release_gate(read_gate, read_resumed)

    assert isinstance(captured.get("exc"), anyio.get_cancelled_exc_class())
    assert file_registry.created_count == 1
    assert file_registry.close_attempt_count == 1
    _assert_all_closed(file_registry)


@pytest.mark.anyio
async def test_cancel_during_seek_closes_every_created_file(
    file_registry: FileRegistry,
) -> None:
    """Cancellation requested at a rolled-over seek closes both created files.

    The first part rolls to disk and is finished alongside the second part's
    header, so two files exist before file1's seek runs in a worker.  As with
    writes, the worker call completes first and cancellation is delivered at
    the parser's next stream-read suspension point.
    """
    seek_entered, seek_gate = threading.Event(), threading.Event()
    read_reached, read_gate, read_resumed = threading.Event(), threading.Event(), threading.Event()
    CountingSpooledFile.block_policy = {"file1": ("seek", seek_entered, seek_gate)}
    chunks = [
        # First part's rolled payload; its seek has not run yet.
        part_header("first", "a.bin") + b"x" * (SMALL_SPOOL * 2),
        # Finish part 1 (another rolled write) and open part 2's file.
        b"z" + b"\r\n" + part_header("second", "b.bin"),
        # Gated read: parser reaches this checkpoint holding two open files.
        b"y" * 4 + b"\r\n" + f"--{BOUNDARY}--\r\n".encode(),
    ]
    stream = ControlledStream(
        chunks,
        gated_chunk_index=2,
        chunk_reached=read_reached,
        chunk_gate=read_gate,
        chunk_resumed=read_resumed,
    )
    parser = make_parser(stream)

    captured: dict[str, BaseException] = {}

    async def runner() -> None:
        try:
            await parser.parse()
        except BaseException as exc:
            # Captured so the task group can finish and we can assert the type.
            captured["exc"] = exc

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(runner)
        await _wait(seek_entered)
        assert file_registry.created_count == 2
        task_group.cancel_scope.cancel()
        await _release_gate(seek_gate)
        await _wait(read_reached)
        await _release_gate(read_gate, read_resumed)

    assert isinstance(captured.get("exc"), anyio.get_cancelled_exc_class())
    assert file_registry.created_count == 2
    assert file_registry.close_attempt_count == 2
    _assert_all_closed(file_registry)


@pytest.mark.anyio
async def test_parser_exception_closes_all_created_files(file_registry: FileRegistry) -> None:
    """A regular parser exception (stream error) closes every created file."""
    chunks = [
        file_part("first", "a.bin", b"x" * 8),
        file_part("second", "b.bin", b"y" * 8, last=True),
    ]
    stream = ControlledStream(chunks, fail_on_end=True)
    parser = make_parser(stream)

    with pytest.raises(RuntimeError, match="injected stream failure"):
        await parser.parse()

    assert file_registry.created_count == 2
    assert file_registry.close_attempt_count == 2
    _assert_all_closed(file_registry)


@pytest.mark.anyio
async def test_invalid_multipart_exception_closes_created_files(
    file_registry: FileRegistry,
) -> None:
    """A malformed stream that makes the parser raise also closes the files."""
    boundary = BOUNDARY.encode()
    chunks = [
        file_part("first", "a.bin", b"x" * 8),
        b"--" + boundary + b"\r\n",
        # A header line containing an illegal header character forces the
        # underlying multipart parser to raise a FormParserError, which
        # MultiPartParser translates into a MultiPartException.
        b'Content-Disposition: form-data; name="second"\r\n',
        b"illegal header line\r\n\r\n",
        b"value\r\n--" + boundary + b"--\r\n",
    ]
    stream = ControlledStream(chunks)
    parser = make_parser(stream)

    with pytest.raises(MultiPartException, match="Invalid multipart data."):
        await parser.parse()

    assert file_registry.created_count == 1
    assert file_registry.close_attempt_count == 1
    _assert_all_closed(file_registry)


@pytest.mark.anyio
async def test_failing_file_close_does_not_leak_other_files(
    file_registry: FileRegistry,
) -> None:
    """One file whose close() raises must not stop the others from closing.

    The original parse failure must remain the propagated exception; the close
    failure is recorded as a note on it instead of replacing it.
    """
    CountingSpooledFile.failing_close_labels = {"file1"}
    chunks = [
        file_part("first", "a.bin", b"x" * 8),
        file_part("second", "b.bin", b"y" * 8, last=True),
    ]
    stream = ControlledStream(chunks, fail_on_end=True)
    parser = make_parser(stream)

    with pytest.raises(RuntimeError, match="injected stream failure") as exc_info:
        await parser.parse()

    # The close failure is reported on the original exception rather than
    # replacing it.
    assert any("injected close failure for file1" in note for note in exc_info.value.__notes__)
    assert file_registry.created_count == 2
    # Every file gets a close attempt, even though the first one raised.
    assert sorted(file_registry.close_attempt_labels) == ["file1", "file2"]
    # The failing file stays open by design (its close genuinely failed), but
    # the healthy sibling must still have been released.
    second_file = next(file for file in file_registry.instances if file.label == "file2")
    assert second_file.closed
    first_file = next(file for file in file_registry.instances if file.label == "file1")
    assert not first_file.closed


@pytest.mark.anyio
async def test_failing_file_close_on_cancellation_closes_other_files(
    file_registry: FileRegistry,
) -> None:
    """Cancellation plus a failing close still releases the remaining files."""
    CountingSpooledFile.failing_close_labels = {"file1"}
    write_entered, write_gate = threading.Event(), threading.Event()
    read_reached, read_gate, read_resumed = threading.Event(), threading.Event(), threading.Event()
    CountingSpooledFile.block_policy = {"file1": ("write", write_entered, write_gate)}
    chunks = [
        part_header("first", "a.bin") + b"x" * (SMALL_SPOOL * 2),
        b"\r\n" + f"--{BOUNDARY}--\r\n".encode(),
    ]
    stream = ControlledStream(
        chunks,
        gated_chunk_index=1,
        chunk_reached=read_reached,
        chunk_gate=read_gate,
        chunk_resumed=read_resumed,
    )
    parser = make_parser(stream)

    captured: dict[str, BaseException] = {}

    async def runner() -> None:
        try:
            await parser.parse()
        except BaseException as exc:
            # Captured so the task group can finish and we can assert the type.
            captured["exc"] = exc

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(runner)
        await _wait(write_entered)
        task_group.cancel_scope.cancel()
        await _release_gate(write_gate)
        await _wait(read_reached)
        await _release_gate(read_gate, read_resumed)

    assert isinstance(captured.get("exc"), anyio.get_cancelled_exc_class())
    assert file_registry.created_count == 1
    assert file_registry.close_attempt_count == 1
    # The only file failed to close, so it genuinely remains open.
    assert not file_registry.instances[0].closed


@pytest.mark.anyio
async def test_successful_parse_hands_open_files_to_application(
    file_registry: FileRegistry,
) -> None:
    """On success the parser must not close anything: ownership transfers.

    The returned :class:`FormData` carries usable, open handles.  Closing the
    request/form through the normal context is what releases the files.
    """
    chunks = [
        file_part("first", "a.bin", b"x" * (SMALL_SPOOL * 2)),
        file_part("second", "b.bin", b"y" * 8, last=True),
    ]
    stream = ControlledStream(chunks)
    parser = make_parser(stream)

    form = await parser.parse()

    assert isinstance(form, FormData)
    assert file_registry.created_count == 2
    # No defensive close on the success path.
    assert file_registry.close_attempt_count == 0
    assert all(not file.closed for file in file_registry.instances)

    uploads = [value for value in form.values() if isinstance(value, UploadFile)]
    assert len(uploads) == 2
    # The handles handed to the application are fully functional.
    assert await uploads[0].read() == b"x" * (SMALL_SPOOL * 2)
    assert await uploads[1].read() == b"y" * 8

    await form.close()
    assert file_registry.close_attempt_count == 2
    _assert_all_closed(file_registry)


@pytest.mark.anyio
async def test_request_context_releases_handed_off_files(
    file_registry: FileRegistry,
) -> None:
    """End-to-end ownership: request.close() releases files the app received."""
    body = b"".join(
        [
            file_part("first", "a.bin", b"x" * (SMALL_SPOOL * 2)),
            file_part("second", "b.bin", b"y" * 8, last=True),
        ]
    )
    messages: list[Message] = [
        {"type": "http.request", "body": body[:40], "more_body": True},
        {"type": "http.request", "body": body[40:]},
    ]

    async def receive() -> Message:
        return messages.pop(0)

    scope: Scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"content-type", CONTENT_TYPE.encode())],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    request = Request(scope, receive)
    form = await request.form()
    assert file_registry.created_count == 2
    assert file_registry.close_attempt_count == 0
    assert all(not file.closed for file in file_registry.instances)
    assert isinstance(form, FormData)

    await request.close()
    assert file_registry.close_attempt_count == 2
    _assert_all_closed(file_registry)
