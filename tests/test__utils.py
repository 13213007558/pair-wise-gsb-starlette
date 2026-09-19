from __future__ import annotations

import functools
from typing import Any
from unittest.mock import create_autospec

import pytest

from starlette._utils import (
    evaluate_preconditions,
    get_route_path,
    is_async_callable,
    parse_entity_tags,
    parse_http_date,
)
from starlette.types import Scope


def test_async_func() -> None:
    async def async_func() -> None: ...  # pragma: no cover

    def func() -> None: ...  # pragma: no cover

    assert is_async_callable(async_func)
    assert not is_async_callable(func)


def test_async_partial() -> None:
    async def async_func(a: Any, b: Any) -> None: ...  # pragma: no cover

    def func(a: Any, b: Any) -> None: ...  # pragma: no cover

    partial = functools.partial(async_func, 1)
    assert is_async_callable(partial)

    partial = functools.partial(func, 1)  # type: ignore
    assert not is_async_callable(partial)


def test_async_method() -> None:
    class Async:
        async def method(self) -> None: ...  # pragma: no cover

    class Sync:
        def method(self) -> None: ...  # pragma: no cover

    assert is_async_callable(Async().method)
    assert not is_async_callable(Sync().method)


def test_async_object_call() -> None:
    class Async:
        async def __call__(self) -> None: ...  # pragma: no cover

    class Sync:
        def __call__(self) -> None: ...  # pragma: no cover

    assert is_async_callable(Async())
    assert not is_async_callable(Sync())


def test_async_partial_object_call() -> None:
    class Async:
        async def __call__(
            self,
            a: Any,
            b: Any,
        ) -> None: ...  # pragma: no cover

    class Sync:
        def __call__(
            self,
            a: Any,
            b: Any,
        ) -> None: ...  # pragma: no cover

    partial = functools.partial(Async(), 1)
    assert is_async_callable(partial)

    partial = functools.partial(Sync(), 1)  # type: ignore
    assert not is_async_callable(partial)


def test_async_nested_partial() -> None:
    async def async_func(
        a: Any,
        b: Any,
    ) -> None: ...  # pragma: no cover

    partial = functools.partial(async_func, b=2)
    nested_partial = functools.partial(partial, a=1)
    assert is_async_callable(nested_partial)


def test_async_mocked_async_function() -> None:
    async def async_func() -> None: ...  # pragma: no cover

    mock = create_autospec(async_func)
    assert is_async_callable(mock)


@pytest.mark.parametrize(
    "scope, expected_result",
    [
        ({"path": "/foo-123/bar", "root_path": "/foo"}, "/foo-123/bar"),
        ({"path": "/foo/bar", "root_path": "/foo"}, "/bar"),
        ({"path": "/foo", "root_path": "/foo"}, ""),
        ({"path": "/foo/bar", "root_path": "/bar"}, "/foo/bar"),
    ],
)
def test_get_route_path(scope: Scope, expected_result: str) -> None:
    assert get_route_path(scope) == expected_result


@pytest.mark.parametrize(
    "value, expected",
    [
        ('"abc"', [(False, '"abc"')]),
        ('W/"abc"', [(True, '"abc"')]),
        ("*", [(False, "*")]),
        ('W/"abc", "123", *', [(True, '"abc"'), (False, '"123"'), (False, "*")]),
        ('  "abc" ,  W/"def" ', [(False, '"abc"'), (True, '"def"')]),
        ("", []),
    ],
)
def test_parse_entity_tags(value: str, expected: list[tuple[bool, str]]) -> None:
    assert parse_entity_tags(value) == expected


@pytest.mark.parametrize("value", ['"unclosed', "abc", '"a" junk', 'W/"a" W/"b"'])
def test_parse_entity_tags_malformed(value: str) -> None:
    assert parse_entity_tags(value) is None


def test_parse_http_date() -> None:
    assert parse_http_date("Thu, 11 Oct 2013 15:30:19 GMT") is not None
    assert parse_http_date("not a date") is None
    assert parse_http_date("") is None


ETAG = '"abc"'
LAST_MODIFIED = "Thu, 11 Oct 2013 15:30:19 GMT"
EARLIER = "Thu, 11 Oct 2013 15:30:18 GMT"
LATER = "Thu, 11 Oct 2013 15:30:20 GMT"
RESPONSE_HEADERS = {"etag": ETAG, "last-modified": LAST_MODIFIED}


@pytest.mark.parametrize(
    "request_headers, method, expected",
    [
        # If-Match uses the strong comparison.
        ({"if-match": ETAG}, "GET", None),
        ({"if-match": f"W/{ETAG}"}, "GET", 412),
        ({"if-match": '"other"'}, "GET", 412),
        ({"if-match": "*"}, "GET", None),
        ({"if-match": f'"other", {ETAG}'}, "GET", None),
        # If-Unmodified-Since is only evaluated when If-Match is absent.
        ({"if-unmodified-since": LATER}, "GET", None),
        ({"if-unmodified-since": EARLIER}, "GET", 412),
        ({"if-unmodified-since": "not a date"}, "GET", None),
        ({"if-match": ETAG, "if-unmodified-since": EARLIER}, "GET", None),
        # If-None-Match uses the weak comparison, and takes precedence over If-Modified-Since.
        ({"if-none-match": ETAG}, "GET", 304),
        ({"if-none-match": f"W/{ETAG}"}, "GET", 304),
        ({"if-none-match": "*"}, "GET", 304),
        ({"if-none-match": '"other"'}, "GET", None),
        ({"if-none-match": ETAG}, "HEAD", 304),
        ({"if-none-match": ETAG}, "POST", 412),
        ({"if-none-match": '"other"', "if-modified-since": LATER}, "GET", None),
        # If-Modified-Since is only evaluated when If-None-Match is absent.
        ({"if-modified-since": LAST_MODIFIED}, "GET", 304),
        ({"if-modified-since": LATER}, "GET", 304),
        ({"if-modified-since": EARLIER}, "GET", None),
        ({"if-modified-since": "not a date"}, "GET", None),
        ({"if-modified-since": LAST_MODIFIED}, "POST", None),
        # If-Match failures take precedence over If-None-Match.
        ({"if-match": '"other"', "if-none-match": ETAG}, "GET", 412),
        # Malformed entity-tag lists are ignored.
        ({"if-match": "junk"}, "GET", None),
        ({"if-none-match": "junk"}, "GET", None),
    ],
)
def test_evaluate_preconditions(request_headers: dict[str, str], method: str, expected: int | None) -> None:
    assert evaluate_preconditions(request_headers, RESPONSE_HEADERS, method) == expected
