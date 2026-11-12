import io
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect
from tests.types import TestClientFactory


def test_staticfiles(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    app = StaticFiles(directory=tmpdir)
    client = test_client_factory(app)
    response = client.get("/example.txt")
    assert response.status_code == 200
    assert response.text == "<file content>"


def test_staticfiles_websocket(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Mount("/static", app=StaticFiles(directory=tmp_path))])
    client = test_client_factory(app)

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/static/example.txt"):
            pass  # pragma: no cover - The connection is rejected before entering the context.

    assert exc.value.code == 1000


def test_staticfiles_with_pathlib(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    path = tmp_path / "example.txt"
    with open(path, "w") as file:
        file.write("<file content>")

    app = StaticFiles(directory=tmp_path)
    client = test_client_factory(app)
    response = client.get("/example.txt")
    assert response.status_code == 200
    assert response.text == "<file content>"


def test_staticfiles_head_with_middleware(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    """
    see https://github.com/Kludex/starlette/pull/935
    """
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("x" * 100)

    async def does_nothing_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        return response

    routes = [Mount("/static", app=StaticFiles(directory=tmpdir), name="static")]
    middleware = [Middleware(BaseHTTPMiddleware, dispatch=does_nothing_middleware)]
    app = Starlette(routes=routes, middleware=middleware)

    client = test_client_factory(app)
    response = client.head("/static/example.txt")
    assert response.status_code == 200
    assert response.headers.get("content-length") == "100"


def test_staticfiles_with_package(test_client_factory: TestClientFactory) -> None:
    app = StaticFiles(packages=["tests"])
    client = test_client_factory(app)
    response = client.get("/example.txt")
    assert response.status_code == 200
    assert response.text == "123\n"

    app = StaticFiles(packages=[("tests", "statics")])
    client = test_client_factory(app)
    response = client.get("/example.txt")
    assert response.status_code == 200
    assert response.text == "123\n"


def test_staticfiles_post(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    response = client.post("/example.txt")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    assert response.text == "Method Not Allowed"


def test_staticfiles_with_directory_returns_404(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    response = client.get("/")
    assert response.status_code == 404
    assert response.text == "Not Found"


def test_staticfiles_with_missing_file_returns_404(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    response = client.get("/404.txt")
    assert response.status_code == 404
    assert response.text == "Not Found"


def test_staticfiles_instantiated_with_missing_directory(tmpdir: Path) -> None:
    with pytest.raises(RuntimeError) as exc_info:
        path = os.path.join(tmpdir, "no_such_directory")
        StaticFiles(directory=path)
    assert "does not exist" in str(exc_info.value)


def test_staticfiles_configured_with_missing_directory(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "no_such_directory")
    app = StaticFiles(directory=path, check_dir=False)
    client = test_client_factory(app)
    with pytest.raises(RuntimeError) as exc_info:
        client.get("/example.txt")
    assert "does not exist" in str(exc_info.value)


def test_staticfiles_configured_with_file_instead_of_directory(
    tmpdir: Path, test_client_factory: TestClientFactory
) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    app = StaticFiles(directory=path, check_dir=False)
    client = test_client_factory(app)
    with pytest.raises(RuntimeError) as exc_info:
        client.get("/example.txt")
    assert "is not a directory" in str(exc_info.value)


def test_staticfiles_config_check_occurs_only_once(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    app = StaticFiles(directory=tmpdir)
    client = test_client_factory(app)
    assert not app.config_checked

    with pytest.raises(HTTPException):
        client.get("/")

    assert app.config_checked

    with pytest.raises(HTTPException):
        client.get("/")


def test_staticfiles_prevents_breaking_out_of_directory(tmpdir: Path) -> None:
    directory = os.path.join(tmpdir, "foo")
    os.mkdir(directory)

    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("outside root dir")

    app = StaticFiles(directory=directory)
    # We can't test this with 'httpx', so we test the app directly here.
    path = app.get_path({"path": "/../example.txt"})
    scope = {"method": "GET"}

    with pytest.raises(HTTPException) as exc_info:
        anyio.run(app.get_response, path, scope)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not Found"


def test_staticfiles_never_read_file_for_head_method(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    app = StaticFiles(directory=tmpdir)
    client = test_client_factory(app)
    response = client.head("/example.txt")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "14"


def test_staticfiles_304_with_etag_match(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    app = StaticFiles(directory=tmpdir)
    client = test_client_factory(app)
    first_resp = client.get("/example.txt")
    assert first_resp.status_code == 200
    last_etag = first_resp.headers["etag"]
    second_resp = client.get("/example.txt", headers={"if-none-match": last_etag})
    assert second_resp.status_code == 304
    assert second_resp.content == b""
    second_resp = client.get("/example.txt", headers={"if-none-match": f'W/{last_etag}, "123"'})
    assert second_resp.status_code == 304
    assert second_resp.content == b""
    second_resp = client.get("/example.txt", headers={"if-none-match": f'"123",\tW/{last_etag}'})
    assert second_resp.status_code == 304
    assert second_resp.content == b""


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("if_none_match", ["*", " \t* \t"])
def test_staticfiles_304_with_if_none_match_wildcard(
    tmp_path: Path,
    test_client_factory: TestClientFactory,
    method: str,
    if_none_match: str,
) -> None:
    (tmp_path / "example.txt").write_text("<file content>", encoding="utf-8")

    app = StaticFiles(directory=tmp_path)
    client = test_client_factory(app)
    response = client.request(method, "/example.txt", headers={"if-none-match": if_none_match})
    assert response.status_code == 304
    assert response.content == b""


@pytest.mark.parametrize("if_none_match", ['"123"', '"*"', '"foo,*,bar"'])
def test_staticfiles_200_with_etag_mismatch(
    tmp_path: Path,
    test_client_factory: TestClientFactory,
    if_none_match: str,
) -> None:
    (tmp_path / "example.txt").write_text("<file content>", encoding="utf-8")

    app = StaticFiles(directory=tmp_path)
    client = test_client_factory(app)
    response = client.get("/example.txt", headers={"if-none-match": if_none_match})
    assert response.status_code == 200
    assert response.content == b"<file content>"


def test_staticfiles_200_with_etag_mismatch_and_timestamp_match(
    tmpdir: Path, test_client_factory: TestClientFactory
) -> None:
    path = tmpdir / "example.txt"
    path.write_text("<file content>", encoding="utf-8")

    app = StaticFiles(directory=tmpdir)
    client = test_client_factory(app)
    first_resp = client.get("/example.txt")
    assert first_resp.status_code == 200
    assert first_resp.headers["etag"] != '"123"'
    last_modified = first_resp.headers["last-modified"]
    # If `if-none-match` is present, `if-modified-since` is ignored.
    second_resp = client.get("/example.txt", headers={"if-none-match": '"123"', "if-modified-since": last_modified})
    assert second_resp.status_code == 200
    assert second_resp.content == b"<file content>"


def test_staticfiles_304_with_last_modified_compare_last_req(
    tmpdir: Path, test_client_factory: TestClientFactory
) -> None:
    path = os.path.join(tmpdir, "example.txt")
    file_last_modified_time = time.mktime(time.strptime("2013-10-10 23:40:00", "%Y-%m-%d %H:%M:%S"))
    with open(path, "w") as file:
        file.write("<file content>")
    os.utime(path, (file_last_modified_time, file_last_modified_time))

    app = StaticFiles(directory=tmpdir)
    client = test_client_factory(app)
    # last modified less than last request, 304
    response = client.get("/example.txt", headers={"If-Modified-Since": "Thu, 11 Oct 2013 15:30:19 GMT"})
    assert response.status_code == 304
    assert response.content == b""
    # last modified greater than last request, 200 with content
    response = client.get("/example.txt", headers={"If-Modified-Since": "Thu, 20 Feb 2012 15:30:19 GMT"})
    assert response.status_code == 200
    assert response.content == b"<file content>"


def test_staticfiles_html_normal(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "404.html")
    with open(path, "w") as file:
        file.write("<h1>Custom not found page</h1>")
    path = os.path.join(tmpdir, "dir")
    os.mkdir(path)
    path = os.path.join(path, "index.html")
    with open(path, "w") as file:
        file.write("<h1>Hello</h1>")

    app = StaticFiles(directory=tmpdir, html=True)
    client = test_client_factory(app)

    response = client.get("/dir/")
    assert response.url == "http://testserver/dir/"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"

    response = client.get("/dir")
    assert response.url == "http://testserver/dir/"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"

    response = client.get("/dir/index.html")
    assert response.url == "http://testserver/dir/index.html"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"

    response = client.get("/missing")
    assert response.status_code == 404
    assert response.text == "<h1>Custom not found page</h1>"


def test_staticfiles_html_without_index(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "404.html")
    with open(path, "w") as file:
        file.write("<h1>Custom not found page</h1>")
    path = os.path.join(tmpdir, "dir")
    os.mkdir(path)

    app = StaticFiles(directory=tmpdir, html=True)
    client = test_client_factory(app)

    response = client.get("/dir/")
    assert response.url == "http://testserver/dir/"
    assert response.status_code == 404
    assert response.text == "<h1>Custom not found page</h1>"

    response = client.get("/dir")
    assert response.url == "http://testserver/dir"
    assert response.status_code == 404
    assert response.text == "<h1>Custom not found page</h1>"

    response = client.get("/missing")
    assert response.status_code == 404
    assert response.text == "<h1>Custom not found page</h1>"


def test_staticfiles_html_without_404(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "dir")
    os.mkdir(path)
    path = os.path.join(path, "index.html")
    with open(path, "w") as file:
        file.write("<h1>Hello</h1>")

    app = StaticFiles(directory=tmpdir, html=True)
    client = test_client_factory(app)

    response = client.get("/dir/")
    assert response.url == "http://testserver/dir/"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"

    response = client.get("/dir")
    assert response.url == "http://testserver/dir/"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"

    with pytest.raises(HTTPException) as exc_info:
        response = client.get("/missing")
    assert exc_info.value.status_code == 404


def test_staticfiles_html_only_files(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "hello.html")
    with open(path, "w") as file:
        file.write("<h1>Hello</h1>")

    app = StaticFiles(directory=tmpdir, html=True)
    client = test_client_factory(app)

    with pytest.raises(HTTPException) as exc_info:
        response = client.get("/")
    assert exc_info.value.status_code == 404

    response = client.get("/hello.html")
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"


def test_staticfiles_cache_invalidation_for_deleted_file_html_mode(
    tmpdir: Path, test_client_factory: TestClientFactory
) -> None:
    path_404 = os.path.join(tmpdir, "404.html")
    with open(path_404, "w") as file:
        file.write("<p>404 file</p>")
    path_some = os.path.join(tmpdir, "some.html")
    with open(path_some, "w") as file:
        file.write("<p>some file</p>")

    common_modified_time = time.mktime(time.strptime("2013-10-10 23:40:00", "%Y-%m-%d %H:%M:%S"))
    os.utime(path_404, (common_modified_time, common_modified_time))
    os.utime(path_some, (common_modified_time, common_modified_time))

    app = StaticFiles(directory=tmpdir, html=True)
    client = test_client_factory(app)

    resp_exists = client.get("/some.html")
    assert resp_exists.status_code == 200
    assert resp_exists.text == "<p>some file</p>"

    resp_cached = client.get(
        "/some.html",
        headers={"If-Modified-Since": resp_exists.headers["last-modified"]},
    )
    assert resp_cached.status_code == 304

    os.remove(path_some)

    resp_deleted = client.get(
        "/some.html",
        headers={"If-Modified-Since": resp_exists.headers["last-modified"]},
    )
    assert resp_deleted.status_code == 404
    assert resp_deleted.text == "<p>404 file</p>"


def test_staticfiles_with_invalid_dir_permissions_returns_401(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "example.txt").write_bytes(b"<file content>")

    original_mode = tmp_path.stat().st_mode
    tmp_path.chmod(stat.S_IRWXO)
    try:
        routes = [
            Mount(
                "/",
                app=StaticFiles(directory=os.fsdecode(tmp_path)),
                name="static",
            )
        ]
        app = Starlette(routes=routes)
        client = test_client_factory(app)

        response = client.get("/example.txt")
        assert response.status_code == 401
        assert response.text == "Unauthorized"
    finally:
        tmp_path.chmod(original_mode)


def test_staticfiles_with_missing_dir_returns_404(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    response = client.get("/foo/example.txt")
    assert response.status_code == 404
    assert response.text == "Not Found"


def test_staticfiles_access_file_as_dir_returns_404(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    response = client.get("/example.txt/foo")
    assert response.status_code == 404
    assert response.text == "Not Found"


def test_staticfiles_null_byte_in_path(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    response = client.get("/example%00.txt")
    assert response.status_code == 404


@pytest.mark.skipif(not hasattr(os, "pathconf"), reason="os.pathconf is Unix-only")
def test_staticfiles_filename_too_long(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    path_max_size = os.pathconf("/", "PC_PATH_MAX")
    response = client.get(f"/{'a' * path_max_size}.txt")
    assert response.status_code == 404
    assert response.text == "Not Found"


def test_staticfiles_unhandled_os_error_returns_500(
    tmpdir: Path,
    test_client_factory: TestClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mock_timeout(*args: Any, **kwargs: Any) -> None:
        raise TimeoutError

    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app, raise_server_exceptions=False)

    monkeypatch.setattr("starlette.staticfiles.StaticFiles.lookup_path", mock_timeout)

    response = client.get("/example.txt")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"


def test_staticfiles_follows_symlinks(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    statics_path = os.path.join(tmpdir, "statics")
    os.mkdir(statics_path)

    source_path = tempfile.mkdtemp()
    source_file_path = os.path.join(source_path, "page.html")
    with open(source_file_path, "w") as file:
        file.write("<h1>Hello</h1>")

    statics_file_path = os.path.join(statics_path, "index.html")
    os.symlink(source_file_path, statics_file_path)

    app = StaticFiles(directory=statics_path, follow_symlink=True)
    client = test_client_factory(app)

    response = client.get("/index.html")
    assert response.url == "http://testserver/index.html"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"


def test_staticfiles_follows_symlink_directories(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    statics_path = os.path.join(tmpdir, "statics")
    statics_html_path = os.path.join(statics_path, "html")
    os.mkdir(statics_path)

    source_path = tempfile.mkdtemp()
    source_file_path = os.path.join(source_path, "page.html")
    with open(source_file_path, "w") as file:
        file.write("<h1>Hello</h1>")

    os.symlink(source_path, statics_html_path)

    app = StaticFiles(directory=statics_path, follow_symlink=True)
    client = test_client_factory(app)

    response = client.get("/html/page.html")
    assert response.url == "http://testserver/html/page.html"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"


def test_staticfiles_disallows_path_traversal_with_symlinks(tmpdir: Path) -> None:
    statics_path = os.path.join(tmpdir, "statics")

    root_source_path = tempfile.mkdtemp()
    source_path = os.path.join(root_source_path, "statics")
    os.mkdir(source_path)

    source_file_path = os.path.join(root_source_path, "index.html")
    with open(source_file_path, "w") as file:
        file.write("<h1>Hello</h1>")

    os.symlink(source_path, statics_path)

    app = StaticFiles(directory=statics_path, follow_symlink=True)
    # We can't test this with 'httpx', so we test the app directly here.
    path = app.get_path({"path": "/../index.html"})
    scope = {"method": "GET"}

    with pytest.raises(HTTPException) as exc_info:
        anyio.run(app.get_response, path, scope)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not Found"


def test_staticfiles_avoids_path_traversal(tmp_path: Path) -> None:
    statics_path = tmp_path / "static"
    statics_disallow_path = tmp_path / "static_disallow"

    statics_path.mkdir()
    statics_disallow_path.mkdir()

    static_index_file = statics_path / "index.html"
    statics_disallow_path_index_file = statics_disallow_path / "index.html"
    static_file = tmp_path / "static1.txt"

    static_index_file.write_text("<h1>Hello</h1>")
    statics_disallow_path_index_file.write_text("<h1>Private</h1>")
    static_file.write_text("Private")

    app = StaticFiles(directory=statics_path)

    # We can't test this with 'httpx', so we test the app directly here.
    path = app.get_path({"path": "/../static1.txt"})
    with pytest.raises(HTTPException) as exc_info:
        anyio.run(app.get_response, path, {"method": "GET"})

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not Found"

    path = app.get_path({"path": "/../static_disallow/index.html"})
    with pytest.raises(HTTPException) as exc_info:
        anyio.run(app.get_response, path, {"method": "GET"})

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not Found"


def test_staticfiles_rejects_absolute_paths(tmp_path: Path) -> None:
    statics_path = tmp_path / "static"
    statics_path.mkdir()
    app = StaticFiles(directory=statics_path)

    full_path, stat_result, root = app.lookup_path("/etc/passwd")
    assert full_path == ""
    assert stat_result is None
    assert root == ""


def test_staticfiles_rejects_absolute_windows_paths(tmp_path: Path) -> None:
    statics_path = tmp_path / "static"
    statics_path.mkdir()
    app = StaticFiles(directory=statics_path)

    full_path, stat_result, root = app.lookup_path("\\\\server\\share")
    assert full_path == ""
    assert stat_result is None
    assert root == ""


def test_staticfiles_self_symlinks(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    statics_path = tmp_path / "statics"
    statics_path.mkdir()

    source_file_path = statics_path / "index.html"
    source_file_path.write_text("<h1>Hello</h1>", encoding="utf-8")

    statics_symlink_path = tmp_path / "statics_symlink"
    statics_symlink_path.symlink_to(statics_path)

    app = StaticFiles(directory=statics_symlink_path, follow_symlink=True)
    client = test_client_factory(app)

    response = client.get("/index.html")
    assert response.url == "http://testserver/index.html"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"


def test_staticfiles_relative_directory_symlinks(test_client_factory: TestClientFactory) -> None:
    app = StaticFiles(directory="tests/statics", follow_symlink=True)
    client = test_client_factory(app)
    response = client.get("/example.txt")
    assert response.status_code == 200
    assert response.text == "123\n"


def test_staticfiles_without_admission_keeps_public_default(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "example.txt").write_text("<file content>", encoding="utf-8")

    app = StaticFiles(directory=tmp_path)
    client = test_client_factory(app)

    response = client.get("/example.txt")
    assert response.status_code == 200
    assert response.text == "<file content>"


def test_staticfiles_admission_allows_and_logs_arguments(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "example.txt").write_text("<file content>", encoding="utf-8")
    calls: list[tuple[str, str]] = []

    def admit(scope: dict, root: str, resource_path: str) -> bool:
        # Identity must come from server-side state placed in the scope, never
        # from self-reported client headers.
        assert scope["user"] == "trusted"
        calls.append((root, resource_path))
        return True

    async def authenticate(scope: dict, receive: Any, send: Any) -> None:
        scope["user"] = "trusted"
        await static_app(scope, receive, send)

    static_app = StaticFiles(directory=tmp_path, admission=admit)
    client = test_client_factory(authenticate)
    response = client.get("/example.txt")

    assert response.status_code == 200
    assert response.text == "<file content>"
    assert calls == [(os.path.realpath(os.fsdecode(tmp_path)), "example.txt")]


def test_staticfiles_async_admission(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    (tmp_path / "example.txt").write_text("<file content>", encoding="utf-8")

    async def admit(scope: dict, root: str, resource_path: str) -> bool:
        return resource_path == "example.txt"

    app = StaticFiles(directory=tmp_path, admission=admit)
    client = test_client_factory(app)

    allowed = client.get("/example.txt")
    assert allowed.status_code == 200
    with pytest.raises(HTTPException) as exc_info:
        client.get("/missing.txt")
    assert exc_info.value.status_code == 404


def test_staticfiles_admission_denial_is_indistinguishable_from_missing_file(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    (tmp_path / "public.txt").write_text("public", encoding="utf-8")

    def admit(scope: dict, root: str, resource_path: str) -> bool:
        return resource_path != "secret.txt"

    app = StaticFiles(directory=tmp_path, admission=admit)
    client = test_client_factory(app)

    with pytest.raises(HTTPException) as denied_info:
        client.get("/secret.txt")
    with pytest.raises(HTTPException) as missing_info:
        client.get("/does-not-exist.txt")

    assert denied_info.value.status_code == missing_info.value.status_code == 404
    assert denied_info.value.detail == missing_info.value.detail == "Not Found"
    assert client.get("/public.txt").status_code == 200


def test_staticfiles_admission_runs_for_head(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    calls: list[str] = []

    def admit(scope: dict, root: str, resource_path: str) -> bool:
        calls.append(resource_path)
        return False

    app = StaticFiles(directory=tmp_path, admission=admit)
    client = test_client_factory(app)

    with pytest.raises(HTTPException) as exc_info:
        client.head("/secret.txt")
    assert exc_info.value.status_code == 404
    assert calls == ["secret.txt"]


def test_staticfiles_admission_runs_before_304(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")

    def deny(scope: dict, root: str, resource_path: str) -> bool:
        return False

    app = StaticFiles(directory=tmp_path, admission=deny)
    client = test_client_factory(app)

    with pytest.raises(HTTPException) as exc_info:
        client.get("/secret.txt", headers={"if-none-match": '"anything"'})
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not Found"


def test_staticfiles_admission_allows_304_after_admission(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "example.txt").write_text("<file content>", encoding="utf-8")

    app = StaticFiles(directory=tmp_path, admission=lambda scope, root, path: True)
    client = test_client_factory(app)

    etag = client.get("/example.txt").headers["etag"]
    response = client.get("/example.txt", headers={"if-none-match": etag})
    assert response.status_code == 304


def test_staticfiles_admission_runs_before_range_response(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "secret.txt").write_text("0123456789", encoding="utf-8")

    app = StaticFiles(directory=tmp_path, admission=lambda scope, root, path: False)
    client = test_client_factory(app)

    with pytest.raises(HTTPException) as exc_info:
        client.get("/secret.txt", headers={"range": "bytes=0-3"})
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not Found"


def test_staticfiles_admission_denial_never_opens_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_client_factory: TestClientFactory
) -> None:
    secret_path = tmp_path / "secret.txt"
    secret_path.write_text("top secret", encoding="utf-8")
    opened: list[str] = []

    real_open = io.open

    def tracking_open(file, mode="r", *args: Any, **kwargs: Any) -> Any:
        opened.append(os.fsdecode(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("io.open", tracking_open)
    monkeypatch.setattr("builtins.open", tracking_open)

    app = StaticFiles(directory=tmp_path, admission=lambda scope, root, path: False)
    client = test_client_factory(app)
    with pytest.raises(HTTPException) as exc_info:
        client.get("/secret.txt")

    assert exc_info.value.status_code == 404
    assert not any(os.path.abspath(path) == os.path.abspath(secret_path) for path in opened)


def test_staticfiles_admission_denial_does_not_reuse_previous_allow(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "example.txt").write_text("<file content>", encoding="utf-8")

    def admit_once(scope: dict, root: str, resource_path: str) -> bool:
        # Allow the first request for this resource, deny every subsequent one.
        if getattr(admit_once, "used", False):
            return False
        admit_once.used = True  # type: ignore[attr-defined]
        return True

    app = StaticFiles(directory=tmp_path, admission=admit_once)
    client = test_client_factory(app)

    first = client.get("/example.txt")
    assert first.status_code == 200
    with pytest.raises(HTTPException) as exc_info:
        client.get("/example.txt")
    assert exc_info.value.status_code == 404


def test_staticfiles_admission_covers_directory_index(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    (tmp_path / "404.html").write_text("<h1>Custom not found page</h1>", encoding="utf-8")
    directory = tmp_path / "dir"
    directory.mkdir()
    (directory / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")
    calls: list[str] = []

    def admit(scope: dict, root: str, resource_path: str) -> bool:
        calls.append(resource_path)
        return not resource_path.startswith("dir")

    app = StaticFiles(directory=tmp_path, html=True, admission=admit)
    client = test_client_factory(app)

    with pytest.raises(HTTPException) as slash_info:
        client.get("/dir/")
    assert slash_info.value.status_code == 404
    with pytest.raises(HTTPException) as no_slash_info:
        client.get("/dir")
    assert no_slash_info.value.status_code == 404
    # The directory redirect must not be issued before admission either.
    assert all(call == "dir/index.html" for call in calls)


def test_staticfiles_admission_allows_directory_index_and_redirect(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    directory = tmp_path / "dir"
    directory.mkdir()
    (directory / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")
    calls: list[str] = []

    app = StaticFiles(
        directory=tmp_path,
        html=True,
        admission=lambda scope, root, path: calls.append(path) or True,
    )
    client = test_client_factory(app)

    response = client.get("/dir")
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"
    # The redirect is followed by the client, so admission runs once per hop,
    # always against the concrete index resource.
    assert calls == ["dir/index.html", "dir/index.html"]


def test_staticfiles_admission_runs_before_directory_redirect(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    directory = tmp_path / "dir"
    directory.mkdir()
    (directory / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")

    app = StaticFiles(directory=tmp_path, html=True, admission=lambda scope, root, path: False)
    client = test_client_factory(app)

    with pytest.raises(HTTPException) as exc_info:
        client.get("/dir", follow_redirects=False)
    assert exc_info.value.status_code == 404


def test_staticfiles_admission_covers_custom_404(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    (tmp_path / "404.html").write_text("<h1>Custom not found page</h1>", encoding="utf-8")
    calls: list[str] = []

    def admit(scope: dict, root: str, resource_path: str) -> bool:
        calls.append(resource_path)
        return resource_path != "404.html"

    app = StaticFiles(directory=tmp_path, html=True, admission=admit)
    client = test_client_factory(app)

    with pytest.raises(HTTPException) as exc_info:
        client.get("/missing")
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not Found"
    assert "404.html" in calls


def test_staticfiles_admission_callback_exception_fails_closed(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "example.txt").write_text("<file content>", encoding="utf-8")

    def boom(scope: dict, root: str, resource_path: str) -> bool:
        raise RuntimeError("authorization backend down")

    app = StaticFiles(directory=tmp_path, admission=boom)
    client = test_client_factory(app, raise_server_exceptions=False)

    response = client.get("/example.txt")
    assert response.status_code == 500
    assert "<file content>" not in response.text


def test_staticfiles_admission_async_exception_fails_closed(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "example.txt").write_text("<file content>", encoding="utf-8")

    async def boom(scope: dict, root: str, resource_path: str) -> bool:
        raise RuntimeError("authorization backend down")

    app = StaticFiles(directory=tmp_path, admission=boom)
    client = test_client_factory(app, raise_server_exceptions=False)

    response = client.get("/example.txt")
    assert response.status_code == 500
    assert "<file content>" not in response.text


def test_staticfiles_admission_receives_normalized_relative_path(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "file.txt").write_text("x", encoding="utf-8")
    seen: list[tuple[str, str]] = []

    app = StaticFiles(
        directory=tmp_path,
        admission=lambda scope, root, path: seen.append((root, path)) or True,
    )
    client = test_client_factory(app)

    response = client.get("/nested/./file.txt")
    assert response.status_code == 200
    assert seen == [(os.path.realpath(os.fsdecode(tmp_path)), os.path.join("nested", "file.txt").replace(os.sep, "/"))]


def test_staticfiles_admission_respects_mount_prefix_and_root_path(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "example.txt").write_text("<file content>", encoding="utf-8")
    seen: list[str] = []

    app = Starlette(
        routes=[
            Mount(
                "/reports",
                app=StaticFiles(
                    directory=tmp_path,
                    admission=lambda scope, root, path: seen.append(path) or True,
                ),
            )
        ]
    )
    client = test_client_factory(app)

    response = client.get("/reports/example.txt")
    assert response.status_code == 200
    assert seen == ["example.txt"]


def test_staticfiles_admission_cannot_trust_client_headers(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")

    def admit(scope: dict, root: str, resource_path: str) -> bool:
        # Deliberately ignore the spoofable header and rely on server state.
        request = Request(scope)
        assert request.headers.get("x-user") == "admin"
        return scope.get("user") == "admin"

    app = StaticFiles(directory=tmp_path, admission=admit)
    client = test_client_factory(app)

    with pytest.raises(HTTPException) as exc_info:
        client.get("/secret.txt", headers={"x-user": "admin"})
    assert exc_info.value.status_code == 404


def test_staticfiles_admission_strips_external_root_path(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "example.txt").write_text("<file content>", encoding="utf-8")
    seen: list[str] = []

    app = StaticFiles(
        directory=tmp_path,
        admission=lambda scope, root, path: seen.append(path) or True,
    )
    client = test_client_factory(app, root_path="/gateway")

    response = client.get("/example.txt")
    assert response.status_code == 200
    assert seen == ["example.txt"]


def test_staticfiles_admission_reports_package_root(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    # Only the package root contains the requested file, so the callback has to
    # receive that root identifier rather than the empty directory.
    seen: list[tuple[str, str]] = []

    app = StaticFiles(
        directory=tmp_path,
        packages=["tests"],
        admission=lambda scope, root, path: seen.append((root, path)) or True,
    )
    client = test_client_factory(app)

    response = client.get("/example.txt")
    assert response.status_code == 200
    assert response.text == "123\n"
    assert seen == [(os.path.realpath(os.path.join("tests", "statics")), "example.txt")]
