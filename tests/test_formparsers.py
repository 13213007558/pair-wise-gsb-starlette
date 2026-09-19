from __future__ import annotations

import os
import threading
from collections.abc import AsyncGenerator, Generator
from contextlib import AbstractContextManager, nullcontext as does_not_raise
from io import BytesIO
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Any, ClassVar
from unittest import mock

import anyio
import pytest

from starlette.applications import Starlette
from starlette.datastructures import FormData, Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser, _user_safe_decode
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send
from tests.types import TestClientFactory


class ForceMultipartDict(dict[Any, Any]):
    def __bool__(self) -> bool:
        return True


# FORCE_MULTIPART is an empty dict that boolean-evaluates as `True`.
FORCE_MULTIPART = ForceMultipartDict()


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    request = Request(scope, receive)
    data = await request.form()
    output: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, UploadFile):
            content = await value.read()
            output[key] = {
                "filename": value.filename,
                "size": value.size,
                "content": content.decode(),
                "content_type": value.content_type,
            }
        else:
            output[key] = value
    await request.close()
    response = JSONResponse(output)
    await response(scope, receive, send)


async def multi_items_app(scope: Scope, receive: Receive, send: Send) -> None:
    request = Request(scope, receive)
    data = await request.form()
    output: dict[str, list[Any]] = {}
    for key, value in data.multi_items():
        if key not in output:
            output[key] = []
        if isinstance(value, UploadFile):
            content = await value.read()
            output[key].append(
                {
                    "filename": value.filename,
                    "size": value.size,
                    "content": content.decode(),
                    "content_type": value.content_type,
                }
            )
        else:
            output[key].append(value)
    await request.close()
    response = JSONResponse(output)
    await response(scope, receive, send)


async def app_with_headers(scope: Scope, receive: Receive, send: Send) -> None:
    request = Request(scope, receive)
    data = await request.form()
    output: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, UploadFile):
            content = await value.read()
            output[key] = {
                "filename": value.filename,
                "size": value.size,
                "content": content.decode(),
                "content_type": value.content_type,
                "headers": list(value.headers.items()),
            }
        else:
            output[key] = value
    await request.close()
    response = JSONResponse(output)
    await response(scope, receive, send)


async def app_read_body(scope: Scope, receive: Receive, send: Send) -> None:
    request = Request(scope, receive)
    # Read bytes, to force request.stream() to return the already parsed body
    await request.body()
    data = await request.form()
    output = {}
    for key, value in data.items():
        output[key] = value
    await request.close()
    response = JSONResponse(output)
    await response(scope, receive, send)


async def app_monitor_thread(scope: Scope, receive: Receive, send: Send) -> None:
    """Helper app to monitor what thread the app was called on.

    This can later be used to validate thread/event loop operations.
    """
    request = Request(scope, receive)

    # Make sure we parse the form
    await request.form()
    await request.close()

    # Send back the current thread id
    response = JSONResponse({"thread_ident": threading.current_thread().ident})
    await response(scope, receive, send)


def make_app_max_parts(max_files: int = 1000, max_fields: int = 1000, max_part_size: int = 1024 * 1024) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        data = await request.form(max_files=max_files, max_fields=max_fields, max_part_size=max_part_size)
        output: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, UploadFile):
                content = await value.read()
                output[key] = {
                    "filename": value.filename,
                    "size": value.size,
                    "content": content.decode(),
                    "content_type": value.content_type,
                }
            else:
                output[key] = value
        await request.close()
        response = JSONResponse(output)
        await response(scope, receive, send)

    return app


def test_multipart_request_data(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app)
    response = client.post("/", data={"some": "data"}, files=FORCE_MULTIPART)
    assert response.json() == {"some": "data"}


def test_multipart_request_files(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "test.txt")
    with open(path, "wb") as file:
        file.write(b"<file content>")

    client = test_client_factory(app)
    with open(path, "rb") as f:
        response = client.post("/", files={"test": f})
        assert response.json() == {
            "test": {
                "filename": "test.txt",
                "size": 14,
                "content": "<file content>",
                "content_type": "text/plain",
            }
        }


def test_multipart_request_files_with_content_type(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "test.txt")
    with open(path, "wb") as file:
        file.write(b"<file content>")

    client = test_client_factory(app)
    with open(path, "rb") as f:
        response = client.post("/", files={"test": ("test.txt", f, "text/plain")})
        assert response.json() == {
            "test": {
                "filename": "test.txt",
                "size": 14,
                "content": "<file content>",
                "content_type": "text/plain",
            }
        }


def test_multipart_request_multiple_files(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path1 = os.path.join(tmpdir, "test1.txt")
    with open(path1, "wb") as file:
        file.write(b"<file1 content>")

    path2 = os.path.join(tmpdir, "test2.txt")
    with open(path2, "wb") as file:
        file.write(b"<file2 content>")

    client = test_client_factory(app)
    with open(path1, "rb") as f1, open(path2, "rb") as f2:
        response = client.post("/", files={"test1": f1, "test2": ("test2.txt", f2, "text/plain")})
        assert response.json() == {
            "test1": {
                "filename": "test1.txt",
                "size": 15,
                "content": "<file1 content>",
                "content_type": "text/plain",
            },
            "test2": {
                "filename": "test2.txt",
                "size": 15,
                "content": "<file2 content>",
                "content_type": "text/plain",
            },
        }


def test_multipart_request_multiple_files_with_headers(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path1 = os.path.join(tmpdir, "test1.txt")
    with open(path1, "wb") as file:
        file.write(b"<file1 content>")

    path2 = os.path.join(tmpdir, "test2.txt")
    with open(path2, "wb") as file:
        file.write(b"<file2 content>")

    client = test_client_factory(app_with_headers)
    with open(path1, "rb") as f1, open(path2, "rb") as f2:
        response = client.post(
            "/",
            files=[
                ("test1", (None, f1)),
                ("test2", ("test2.txt", f2, "text/plain", {"x-custom": "f2"})),
            ],
        )
        assert response.json() == {
            "test1": "<file1 content>",
            "test2": {
                "filename": "test2.txt",
                "size": 15,
                "content": "<file2 content>",
                "content_type": "text/plain",
                "headers": [
                    [
                        "content-disposition",
                        'form-data; name="test2"; filename="test2.txt"',
                    ],
                    ["x-custom", "f2"],
                    ["content-type", "text/plain"],
                ],
            },
        }


def test_multi_items(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path1 = os.path.join(tmpdir, "test1.txt")
    with open(path1, "wb") as file:
        file.write(b"<file1 content>")

    path2 = os.path.join(tmpdir, "test2.txt")
    with open(path2, "wb") as file:
        file.write(b"<file2 content>")

    client = test_client_factory(multi_items_app)
    with open(path1, "rb") as f1, open(path2, "rb") as f2:
        response = client.post(
            "/",
            data={"test1": "abc"},
            files=[("test1", f1), ("test1", ("test2.txt", f2, "text/plain"))],
        )
        assert response.json() == {
            "test1": [
                "abc",
                {
                    "filename": "test1.txt",
                    "size": 15,
                    "content": "<file1 content>",
                    "content_type": "text/plain",
                },
                {
                    "filename": "test2.txt",
                    "size": 15,
                    "content": "<file2 content>",
                    "content_type": "text/plain",
                },
            ]
        }


def test_multipart_request_mixed_files_and_data(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app)
    response = client.post(
        "/",
        data=(
            # data
            b"--a7f7ac8d4e2e437c877bb7b8d7cc549c\r\n"  # type: ignore
            b'Content-Disposition: form-data; name="field0"\r\n\r\n'
            b"value0\r\n"
            # file
            b"--a7f7ac8d4e2e437c877bb7b8d7cc549c\r\n"
            b'Content-Disposition: form-data; name="file"; filename="file.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
            b"<file content>\r\n"
            # data
            b"--a7f7ac8d4e2e437c877bb7b8d7cc549c\r\n"
            b'Content-Disposition: form-data; name="field1"\r\n\r\n'
            b"value1\r\n"
            b"--a7f7ac8d4e2e437c877bb7b8d7cc549c--\r\n"
        ),
        headers={"Content-Type": ("multipart/form-data; boundary=a7f7ac8d4e2e437c877bb7b8d7cc549c")},
    )
    assert response.json() == {
        "file": {
            "filename": "file.txt",
            "size": 14,
            "content": "<file content>",
            "content_type": "text/plain",
        },
        "field0": "value0",
        "field1": "value1",
    }


class ThreadTrackingSpooledTemporaryFile(SpooledTemporaryFile[bytes]):
    """Helper class to track which threads performed the rollover operation.

    This is not threadsafe/multi-test safe.
    """

    rollover_threads: ClassVar[set[int | None]] = set()

    def rollover(self) -> None:
        ThreadTrackingSpooledTemporaryFile.rollover_threads.add(threading.current_thread().ident)
        super().rollover()


@pytest.fixture
def mock_spooled_temporary_file() -> Generator[None]:
    try:
        with mock.patch("starlette.formparsers.SpooledTemporaryFile", ThreadTrackingSpooledTemporaryFile):
            yield
    finally:
        ThreadTrackingSpooledTemporaryFile.rollover_threads.clear()


def test_multipart_request_large_file_rollover_in_background_thread(
    mock_spooled_temporary_file: None, test_client_factory: TestClientFactory
) -> None:
    """Test that Spooled file rollovers happen in background threads."""
    data = BytesIO(b" " * (MultiPartParser.spool_max_size + 1))

    client = test_client_factory(app_monitor_thread)
    response = client.post("/", files=[("test_large", data)])
    assert response.status_code == 200

    # Parse the event thread id from the API response and ensure we have one
    app_thread_ident = response.json().get("thread_ident")
    assert app_thread_ident is not None

    # Ensure the app thread was not the same as the rollover one and that a rollover thread exists
    assert app_thread_ident not in ThreadTrackingSpooledTemporaryFile.rollover_threads
    assert len(ThreadTrackingSpooledTemporaryFile.rollover_threads) == 1


def test_multipart_request_with_charset_for_filename(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app)
    response = client.post(
        "/",
        data=(
            # file
            b"--a7f7ac8d4e2e437c877bb7b8d7cc549c\r\n"  # type: ignore
            b'Content-Disposition: form-data; name="file"; filename="\xe6\x96\x87\xe6\x9b\xb8.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
            b"<file content>\r\n"
            b"--a7f7ac8d4e2e437c877bb7b8d7cc549c--\r\n"
        ),
        headers={"Content-Type": ("multipart/form-data; charset=utf-8; boundary=a7f7ac8d4e2e437c877bb7b8d7cc549c")},
    )
    assert response.json() == {
        "file": {
            "filename": "文書.txt",
            "size": 14,
            "content": "<file content>",
            "content_type": "text/plain",
        }
    }


def test_multipart_request_without_charset_for_filename(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app)
    response = client.post(
        "/",
        data=(
            # file
            b"--a7f7ac8d4e2e437c877bb7b8d7cc549c\r\n"  # type: ignore
            b'Content-Disposition: form-data; name="file"; filename="\xe7\x94\xbb\xe5\x83\x8f.jpg"\r\n'
            b"Content-Type: image/jpeg\r\n\r\n"
            b"<file content>\r\n"
            b"--a7f7ac8d4e2e437c877bb7b8d7cc549c--\r\n"
        ),
        headers={"Content-Type": ("multipart/form-data; boundary=a7f7ac8d4e2e437c877bb7b8d7cc549c")},
    )
    assert response.json() == {
        "file": {
            "filename": "画像.jpg",
            "size": 14,
            "content": "<file content>",
            "content_type": "image/jpeg",
        }
    }


def test_multipart_request_with_encoded_value(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app)
    response = client.post(
        "/",
        data=(
            b"--20b303e711c4ab8c443184ac833ab00f\r\n"  # type: ignore
            b"Content-Disposition: form-data; "
            b'name="value"\r\n\r\n'
            b"Transf\xc3\xa9rer\r\n"
            b"--20b303e711c4ab8c443184ac833ab00f--\r\n"
        ),
        headers={"Content-Type": ("multipart/form-data; charset=utf-8; boundary=20b303e711c4ab8c443184ac833ab00f")},
    )
    assert response.json() == {"value": "Transférer"}


def test_urlencoded_request_data(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app)
    response = client.post("/", data={"some": "data"})
    assert response.json() == {"some": "data"}


def test_no_request_data(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app)
    response = client.post("/")
    assert response.json() == {}


def test_urlencoded_percent_encoding(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app)
    response = client.post("/", data={"some": "da ta"})
    assert response.json() == {"some": "da ta"}


def test_urlencoded_percent_encoding_keys(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app)
    response = client.post("/", data={"so me": "data"})
    assert response.json() == {"so me": "data"}


def test_urlencoded_multi_field_app_reads_body(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app_read_body)
    response = client.post("/", data={"some": "data", "second": "key pair"})
    assert response.json() == {"some": "data", "second": "key pair"}


def test_multipart_multi_field_app_reads_body(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app_read_body)
    response = client.post("/", data={"some": "data", "second": "key pair"}, files=FORCE_MULTIPART)
    assert response.json() == {"some": "data", "second": "key pair"}


def test_user_safe_decode_helper() -> None:
    result = _user_safe_decode(b"\xc4\x99\xc5\xbc\xc4\x87", "utf-8")
    assert result == "ężć"


def test_user_safe_decode_ignores_wrong_charset() -> None:
    result = _user_safe_decode(b"abc", "latin-8")
    assert result == "abc"


@pytest.mark.parametrize(
    "app,expectation",
    [
        (app, pytest.raises(MultiPartException)),
        (Starlette(routes=[Mount("/", app=app)]), does_not_raise()),
    ],
)
def test_missing_boundary_parameter(
    app: ASGIApp,
    expectation: AbstractContextManager[Exception],
    test_client_factory: TestClientFactory,
) -> None:
    client = test_client_factory(app)
    with expectation:
        res = client.post(
            "/",
            data=(
                # file
                b'Content-Disposition: form-data; name="file"; filename="\xe6\x96\x87\xe6\x9b\xb8.txt"\r\n'  # type: ignore
                b"Content-Type: text/plain\r\n\r\n"
                b"<file content>\r\n"
            ),
            headers={"Content-Type": "multipart/form-data; charset=utf-8"},
        )
        assert res.status_code == 400
        assert res.text == "Missing boundary in multipart."


@pytest.mark.parametrize(
    "app,expectation",
    [
        (app, pytest.raises(MultiPartException)),
        (Starlette(routes=[Mount("/", app=app)]), does_not_raise()),
    ],
)
def test_missing_name_parameter_on_content_disposition(
    app: ASGIApp,
    expectation: AbstractContextManager[Exception],
    test_client_factory: TestClientFactory,
) -> None:
    client = test_client_factory(app)
    with expectation:
        res = client.post(
            "/",
            data=(
                # data
                b"--a7f7ac8d4e2e437c877bb7b8d7cc549c\r\n"  # type: ignore
                b'Content-Disposition: form-data; ="field0"\r\n\r\n'
                b"value0\r\n"
            ),
            headers={"Content-Type": ("multipart/form-data; boundary=a7f7ac8d4e2e437c877bb7b8d7cc549c")},
        )
        assert res.status_code == 400
        assert res.text == 'The Content-Disposition header field "name" must be provided.'


@pytest.mark.parametrize(
    "app,expectation",
    [
        (app, pytest.raises(MultiPartException)),
        (Starlette(routes=[Mount("/", app=app)]), does_not_raise()),
    ],
)
def test_too_many_fields_raise(
    app: ASGIApp,
    expectation: AbstractContextManager[Exception],
    test_client_factory: TestClientFactory,
) -> None:
    client = test_client_factory(app)
    fields = []
    for i in range(1001):
        fields.append(f'--B\r\nContent-Disposition: form-data; name="N{i}";\r\n\r\n\r\n')
    data = "".join(fields).encode("utf-8")
    with expectation:
        res = client.post(
            "/",
            data=data,  # type: ignore
            headers={"Content-Type": ("multipart/form-data; boundary=B")},
        )
        assert res.status_code == 400
        assert res.text == "Too many fields. Maximum number of fields is 1000."


@pytest.mark.parametrize(
    "app,expectation",
    [
        (app, pytest.raises(MultiPartException)),
        (Starlette(routes=[Mount("/", app=app)]), does_not_raise()),
    ],
)
def test_too_many_files_raise(
    app: ASGIApp,
    expectation: AbstractContextManager[Exception],
    test_client_factory: TestClientFactory,
) -> None:
    client = test_client_factory(app)
    fields = []
    for i in range(1001):
        fields.append(f'--B\r\nContent-Disposition: form-data; name="N{i}"; filename="F{i}";\r\n\r\n\r\n')
    data = "".join(fields).encode("utf-8")
    with expectation:
        res = client.post(
            "/",
            data=data,  # type: ignore
            headers={"Content-Type": ("multipart/form-data; boundary=B")},
        )
        assert res.status_code == 400
        assert res.text == "Too many files. Maximum number of files is 1000."


@pytest.mark.parametrize(
    "app,expectation",
    [
        (app, pytest.raises(MultiPartException)),
        (Starlette(routes=[Mount("/", app=app)]), does_not_raise()),
    ],
)
def test_too_many_files_single_field_raise(
    app: ASGIApp,
    expectation: AbstractContextManager[Exception],
    test_client_factory: TestClientFactory,
) -> None:
    client = test_client_factory(app)
    fields = []
    for i in range(1001):
        # This uses the same field name "N" for all files, equivalent to a
        # multifile upload form field
        fields.append(f'--B\r\nContent-Disposition: form-data; name="N"; filename="F{i}";\r\n\r\n\r\n')
    data = "".join(fields).encode("utf-8")
    with expectation:
        res = client.post(
            "/",
            data=data,  # type: ignore
            headers={"Content-Type": ("multipart/form-data; boundary=B")},
        )
        assert res.status_code == 400
        assert res.text == "Too many files. Maximum number of files is 1000."


@pytest.mark.parametrize(
    "app,expectation",
    [
        (app, pytest.raises(MultiPartException)),
        (Starlette(routes=[Mount("/", app=app)]), does_not_raise()),
    ],
)
def test_too_many_files_and_fields_raise(
    app: ASGIApp,
    expectation: AbstractContextManager[Exception],
    test_client_factory: TestClientFactory,
) -> None:
    client = test_client_factory(app)
    fields = []
    for i in range(1001):
        fields.append(f'--B\r\nContent-Disposition: form-data; name="F{i}"; filename="F{i}";\r\n\r\n\r\n')
        fields.append(f'--B\r\nContent-Disposition: form-data; name="N{i}";\r\n\r\n\r\n')
    data = "".join(fields).encode("utf-8")
    with expectation:
        res = client.post(
            "/",
            data=data,  # type: ignore
            headers={"Content-Type": ("multipart/form-data; boundary=B")},
        )
        assert res.status_code == 400
        assert res.text == "Too many files. Maximum number of files is 1000."


@pytest.mark.parametrize(
    "app,expectation",
    [
        (make_app_max_parts(max_fields=1), pytest.raises(MultiPartException)),
        (
            Starlette(routes=[Mount("/", app=make_app_max_parts(max_fields=1))]),
            does_not_raise(),
        ),
    ],
)
def test_max_fields_is_customizable_low_raises(
    app: ASGIApp,
    expectation: AbstractContextManager[Exception],
    test_client_factory: TestClientFactory,
) -> None:
    client = test_client_factory(app)
    fields = []
    for i in range(2):
        fields.append(f'--B\r\nContent-Disposition: form-data; name="N{i}";\r\n\r\n\r\n')
    data = "".join(fields).encode("utf-8")
    with expectation:
        res = client.post(
            "/",
            data=data,  # type: ignore
            headers={"Content-Type": ("multipart/form-data; boundary=B")},
        )
        assert res.status_code == 400
        assert res.text == "Too many fields. Maximum number of fields is 1."


@pytest.mark.parametrize(
    "app,expectation",
    [
        (make_app_max_parts(max_files=1), pytest.raises(MultiPartException)),
        (
            Starlette(routes=[Mount("/", app=make_app_max_parts(max_files=1))]),
            does_not_raise(),
        ),
    ],
)
def test_max_files_is_customizable_low_raises(
    app: ASGIApp,
    expectation: AbstractContextManager[Exception],
    test_client_factory: TestClientFactory,
) -> None:
    client = test_client_factory(app)
    fields = []
    for i in range(2):
        fields.append(f'--B\r\nContent-Disposition: form-data; name="F{i}"; filename="F{i}";\r\n\r\n\r\n')
    data = "".join(fields).encode("utf-8")
    with expectation:
        res = client.post(
            "/",
            data=data,  # type: ignore
            headers={"Content-Type": ("multipart/form-data; boundary=B")},
        )
        assert res.status_code == 400
        assert res.text == "Too many files. Maximum number of files is 1."


def test_max_fields_is_customizable_high(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(make_app_max_parts(max_fields=2000, max_files=2000))
    fields = []
    for i in range(2000):
        fields.append(f'--B\r\nContent-Disposition: form-data; name="N{i}";\r\n\r\n\r\n')
        fields.append(f'--B\r\nContent-Disposition: form-data; name="F{i}"; filename="F{i}";\r\n\r\n\r\n')
    data = "".join(fields).encode("utf-8")
    data += b"--B--\r\n"
    res = client.post(
        "/",
        data=data,  # type: ignore
        headers={"Content-Type": ("multipart/form-data; boundary=B")},
    )
    assert res.status_code == 200
    res_data = res.json()
    assert res_data["N1999"] == ""
    assert res_data["F1999"] == {
        "filename": "F1999",
        "size": 0,
        "content": "",
        "content_type": None,
    }


@pytest.mark.parametrize(
    "app,expectation",
    [
        (app, pytest.raises(MultiPartException)),
        (Starlette(routes=[Mount("/", app=app)]), does_not_raise()),
    ],
)
def test_max_part_size_exceeds_limit(
    app: ASGIApp,
    expectation: AbstractContextManager[Exception],
    test_client_factory: TestClientFactory,
) -> None:
    client = test_client_factory(app)
    boundary = "------------------------4K1ON9fZkj9uCUmqLHRbbR"

    multipart_data = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="small"\r\n\r\n'
        "small content\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="large"\r\n\r\n'
        + ("x" * 1024 * 1024 + "x")  # 1MB + 1 byte of data
        + "\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Transfer-Encoding": "chunked",
    }

    with expectation:
        response = client.post("/", data=multipart_data, headers=headers)  # type: ignore
        assert response.status_code == 400
        assert response.text == "Part exceeded maximum size of 1024KB."


@pytest.mark.parametrize(
    "app,expectation",
    [
        (make_app_max_parts(max_part_size=1024 * 10), pytest.raises(MultiPartException)),
        (
            Starlette(routes=[Mount("/", app=make_app_max_parts(max_part_size=1024 * 10))]),
            does_not_raise(),
        ),
    ],
)
def test_max_part_size_exceeds_custom_limit(
    app: ASGIApp,
    expectation: AbstractContextManager[Exception],
    test_client_factory: TestClientFactory,
) -> None:
    client = test_client_factory(app)
    boundary = "------------------------4K1ON9fZkj9uCUmqLHRbbR"

    multipart_data = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="small"\r\n\r\n'
        "small content\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="large"\r\n\r\n'
        + ("x" * 1024 * 10 + "x")  # 1MB + 1 byte of data
        + "\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Transfer-Encoding": "chunked",
    }

    with expectation:
        response = client.post("/", content=multipart_data, headers=headers)
        assert response.status_code == 400
        assert response.text == "Part exceeded maximum size of 10KB."


def make_app_max_sizes(max_file_size: int | None = None, max_body_size: int | None = None) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        data = await request.form(max_file_size=max_file_size, max_body_size=max_body_size)
        output: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, UploadFile):
                content = await value.read()
                output[key] = {
                    "filename": value.filename,
                    "size": value.size,
                    "content": content.decode(),
                    "content_type": value.content_type,
                }
            else:
                output[key] = value
        await request.close()
        response = JSONResponse(output)
        await response(scope, receive, send)

    return app


def encode_multipart(boundary: str, parts: list[tuple[str, bytes, str | None]]) -> bytes:
    """Encode parts as (name, content, filename) tuples into a multipart body."""
    chunks = []
    for name, content, filename in parts:
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        chunks.append(f"--{boundary}\r\n{disposition}\r\n\r\n".encode() + content + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


def multipart_headers(boundary: str, content_length: int | None = None) -> Headers:
    raw = [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())]
    if content_length is not None:
        raw.append((b"content-length", str(content_length).encode()))
    return Headers(raw=raw)


def chunked_stream(body: bytes, chunk_size: int, include_empty_chunks: bool = False) -> AsyncGenerator[bytes, None]:
    async def stream() -> AsyncGenerator[bytes, None]:
        for index in range(0, len(body), chunk_size):
            if include_empty_chunks:
                yield b""
            yield body[index : index + chunk_size]
        if include_empty_chunks:
            yield b""

    return stream()


@pytest.mark.anyio
async def test_max_body_size_multiple_files_exceeds_limit() -> None:
    boundary = "boundary"
    body = encode_multipart(
        boundary,
        [
            ("file1", b"a" * 512, "file1.txt"),
            ("file2", b"b" * 512, "file2.txt"),
        ],
    )
    # No single part exceeds a limit, but the combined raw body does.
    stream = chunked_stream(body, 64)
    parser = MultiPartParser(
        multipart_headers(boundary),
        stream,
        max_body_size=len(body) - 1,
    )
    with pytest.raises(MultiPartException, match=f"Request body exceeded maximum size of {len(body) - 1} bytes."):
        await parser.parse()
    await stream.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("chunk_size", [1, 7, 1024])
async def test_max_body_size_equal_to_limit_succeeds(chunk_size: int) -> None:
    boundary = "boundary"
    body = encode_multipart(
        boundary,
        [
            ("field", b"some value", None),
            ("file", b"f" * 128, "file.txt"),
        ],
    )
    parser = MultiPartParser(
        multipart_headers(boundary),
        chunked_stream(body, chunk_size, include_empty_chunks=True),
        max_body_size=len(body),
    )
    form = await parser.parse()
    assert form["field"] == "some value"
    upload = form["file"]
    assert isinstance(upload, UploadFile)
    assert upload.size == 128
    await form.close()


@pytest.mark.anyio
@pytest.mark.parametrize("content_length", [None, 5])
async def test_max_body_size_ignores_content_length(content_length: int | None) -> None:
    # The limit is enforced from the actual bytes received, so a missing or
    # forged (too small) Content-Length header does not affect it.
    boundary = "boundary"
    body = encode_multipart(boundary, [("file", b"x" * 256, "file.txt")])
    stream = chunked_stream(body, 32)
    parser = MultiPartParser(
        multipart_headers(boundary, content_length=content_length),
        stream,
        max_body_size=128,
    )
    with pytest.raises(MultiPartException, match="Request body exceeded maximum size of 128 bytes."):
        await parser.parse()
    await stream.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("chunk_size", [1, 13, 1024])
async def test_max_file_size_only_one_file_exceeds(chunk_size: int) -> None:
    boundary = "boundary"
    body = encode_multipart(
        boundary,
        [
            ("small", b"s" * 100, "small.txt"),
            ("large", b"L" * 101, "large.txt"),
        ],
    )
    stream = chunked_stream(body, chunk_size, include_empty_chunks=True)
    parser = MultiPartParser(
        multipart_headers(boundary),
        stream,
        max_file_size=100,
    )
    with pytest.raises(MultiPartException, match="File exceeded maximum size of 100 bytes."):
        await parser.parse()
    await stream.aclose()


@pytest.mark.anyio
async def test_max_file_size_equal_to_limit_succeeds() -> None:
    boundary = "boundary"
    body = encode_multipart(
        boundary,
        [
            ("file", b"a" * 100, "a.txt"),
            ("file", b"b" * 100, "b.txt"),  # repeated field name
        ],
    )
    parser = MultiPartParser(multipart_headers(boundary), chunked_stream(body, 3), max_file_size=100)
    form = await parser.parse()
    uploads = [value for _, value in form.multi_items()]
    assert [upload.size for upload in uploads if isinstance(upload, UploadFile)] == [100, 100]
    await form.close()


@pytest.mark.anyio
async def test_max_file_size_counts_file_content_only() -> None:
    # Boundaries and headers count towards the body, but not towards the file limit.
    boundary = "boundary"
    body = encode_multipart(boundary, [("file", b"x" * 100, "file.txt")])
    assert len(body) > 100
    parser = MultiPartParser(multipart_headers(boundary), chunked_stream(body, 16), max_file_size=100)
    form = await parser.parse()
    upload = form["file"]
    assert isinstance(upload, UploadFile)
    assert upload.size == 100
    await form.close()


@pytest.mark.anyio
async def test_max_body_size_closes_spooled_files_on_disk() -> None:
    boundary = "boundary"
    body = encode_multipart(
        boundary,
        [
            ("file1", b"a" * 512, "file1.txt"),
            ("file2", b"b" * 512, "file2.txt"),
        ],
    )
    stream = chunked_stream(body, 64)
    parser = MultiPartParser(multipart_headers(boundary), stream, max_body_size=len(body) - 1)
    parser.spool_max_size = 10  # force the temporary files to roll over to disk
    with pytest.raises(MultiPartException, match="Request body exceeded maximum size"):
        await parser.parse()
    await stream.aclose()
    assert parser._files_to_close_on_error
    for file in parser._files_to_close_on_error:
        assert file._rolled  # the file content was actually written to disk
        assert file.closed


@pytest.mark.anyio
async def test_max_file_size_closes_spooled_files_on_disk() -> None:
    boundary = "boundary"
    body = encode_multipart(boundary, [("file", b"x" * 512, "file.txt")])
    stream = chunked_stream(body, 64)
    parser = MultiPartParser(multipart_headers(boundary), stream, max_file_size=100)
    parser.spool_max_size = 10
    with pytest.raises(MultiPartException, match="File exceeded maximum size of 100 bytes."):
        await parser.parse()
    await stream.aclose()
    assert parser._files_to_close_on_error
    for file in parser._files_to_close_on_error:
        assert file._rolled
        assert file.closed


@pytest.mark.anyio
async def test_files_closed_on_stream_error() -> None:
    boundary = "boundary"
    first_chunk = encode_multipart(boundary, [("file", b"x" * 100, "file.txt")])[:200]

    async def stream() -> AsyncGenerator[bytes, None]:
        yield first_chunk
        raise ClientDisconnect()

    parser = MultiPartParser(multipart_headers(boundary), stream())
    parser.spool_max_size = 10
    with pytest.raises(ClientDisconnect):
        await parser.parse()
    assert parser._files_to_close_on_error
    for file in parser._files_to_close_on_error:
        assert file.closed


@pytest.mark.anyio
async def test_files_closed_and_cancellation_not_converted_on_cancel() -> None:
    boundary = "boundary"
    first_chunk = (
        f"--{boundary}\r\n" f'Content-Disposition: form-data; name="file"; filename="file.txt"\r\n\r\n'
    ).encode() + b"x" * 100
    chunk_processed = anyio.Event()

    async def stream() -> AsyncGenerator[bytes, None]:
        yield first_chunk
        chunk_processed.set()
        await anyio.sleep_forever()

    parser = MultiPartParser(multipart_headers(boundary), stream(), max_body_size=1024, max_file_size=1024)
    parser.spool_max_size = 10
    caught: list[BaseException] = []

    async with anyio.create_task_group() as task_group:

        async def parse() -> None:
            try:
                await parser.parse()
            except BaseException as exc:
                caught.append(exc)
                raise

        async def cancel_when_stalled() -> None:
            await chunk_processed.wait()
            task_group.cancel_scope.cancel()

        task_group.start_soon(parse)
        task_group.start_soon(cancel_when_stalled)

    # The cancellation must propagate as-is, not be converted into a 413 error.
    assert len(caught) == 1
    assert isinstance(caught[0], anyio.get_cancelled_exc_class())
    assert parser._files_to_close_on_error
    for file in parser._files_to_close_on_error:
        assert file.closed


@pytest.mark.anyio
async def test_form_data_close_closes_upload_files() -> None:
    boundary = "boundary"
    body = encode_multipart(
        boundary,
        [
            ("field", b"value", None),
            ("file", b"x" * 100, "file.txt"),
        ],
    )
    parser = MultiPartParser(multipart_headers(boundary), chunked_stream(body, 16))
    parser.spool_max_size = 10
    form = await parser.parse()
    assert isinstance(form, FormData)
    upload = form["file"]
    assert isinstance(upload, UploadFile)
    assert upload.file._rolled
    await form.close()
    assert upload.file.closed


@pytest.mark.parametrize(
    "app,expectation",
    [
        (make_app_max_sizes(max_body_size=512), pytest.raises(MultiPartException)),
        (
            Starlette(routes=[Mount("/", app=make_app_max_sizes(max_body_size=512))]),
            does_not_raise(),
        ),
    ],
)
def test_max_body_size_request_form_returns_413(
    app: ASGIApp,
    expectation: AbstractContextManager[Exception],
    test_client_factory: TestClientFactory,
) -> None:
    client = test_client_factory(app)
    boundary = "boundary"
    multipart_data = encode_multipart(
        boundary,
        [
            ("file1", b"a" * 300, "file1.txt"),
            ("file2", b"b" * 300, "file2.txt"),
        ],
    )
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Transfer-Encoding": "chunked",
    }
    with expectation:
        response = client.post("/", content=multipart_data, headers=headers)
        assert response.status_code == 413
        assert response.text == "Request body exceeded maximum size of 512 bytes."


@pytest.mark.parametrize(
    "app,expectation",
    [
        (make_app_max_sizes(max_file_size=100), pytest.raises(MultiPartException)),
        (
            Starlette(routes=[Mount("/", app=make_app_max_sizes(max_file_size=100))]),
            does_not_raise(),
        ),
    ],
)
def test_max_file_size_request_form_returns_413(
    app: ASGIApp,
    expectation: AbstractContextManager[Exception],
    test_client_factory: TestClientFactory,
) -> None:
    client = test_client_factory(app)
    boundary = "boundary"
    multipart_data = encode_multipart(
        boundary,
        [
            ("small", b"s" * 100, "small.txt"),
            ("large", b"L" * 101, "large.txt"),
        ],
    )
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Transfer-Encoding": "chunked",
    }
    with expectation:
        response = client.post("/", content=multipart_data, headers=headers)
        assert response.status_code == 413
        assert response.text == "File exceeded maximum size of 100 bytes."


def test_request_form_within_size_limits(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(make_app_max_sizes(max_file_size=1024, max_body_size=1024 * 1024))
    path = os.path.join(tmpdir, "test.txt")
    with open(path, "wb") as file:
        file.write(b"<file content>")
    with open(path, "rb") as f:
        response = client.post("/", data={"some": "data"}, files={"test": f})
        assert response.status_code == 200
        assert response.json() == {
            "some": "data",
            "test": {
                "filename": "test.txt",
                "size": 14,
                "content": "<file content>",
                "content_type": "text/plain",
            },
        }
