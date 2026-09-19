from __future__ import annotations

import functools
import re
import sys
from collections.abc import Awaitable, Generator, Mapping
from contextlib import AbstractAsyncContextManager, contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Generic, Protocol, TypeVar, overload

from starlette.types import Scope

if sys.version_info >= (3, 13):  # pragma: no cover
    from inspect import iscoroutinefunction
    from typing import TypeIs
else:  # pragma: no cover
    from asyncio import iscoroutinefunction

    from typing_extensions import TypeIs

has_exceptiongroups = True
if sys.version_info < (3, 11):  # pragma: no cover
    try:
        from exceptiongroup import BaseExceptionGroup  # type: ignore[unused-ignore,import-not-found]
    except ImportError:
        has_exceptiongroups = False

T = TypeVar("T")
AwaitableCallable = Callable[..., Awaitable[T]]


@overload
def is_async_callable(obj: AwaitableCallable[T]) -> TypeIs[AwaitableCallable[T]]: ...


@overload
def is_async_callable(obj: Any) -> TypeIs[AwaitableCallable[Any]]: ...


def is_async_callable(obj: Any) -> Any:
    while isinstance(obj, functools.partial):
        obj = obj.func

    return iscoroutinefunction(obj) or (callable(obj) and iscoroutinefunction(obj.__call__))


T_co = TypeVar("T_co", covariant=True)


class AwaitableOrContextManager(Awaitable[T_co], AbstractAsyncContextManager[T_co], Protocol[T_co]): ...


class SupportsAsyncClose(Protocol):
    async def close(self) -> None: ...  # pragma: no cover


SupportsAsyncCloseType = TypeVar("SupportsAsyncCloseType", bound=SupportsAsyncClose, covariant=False)


class AwaitableOrContextManagerWrapper(Generic[SupportsAsyncCloseType]):
    __slots__ = ("aw", "entered")

    def __init__(self, aw: Awaitable[SupportsAsyncCloseType]) -> None:
        self.aw = aw

    def __await__(self) -> Generator[Any, None, SupportsAsyncCloseType]:
        return self.aw.__await__()

    async def __aenter__(self) -> SupportsAsyncCloseType:
        self.entered = await self.aw
        return self.entered

    async def __aexit__(self, *args: Any) -> None | bool:
        await self.entered.close()
        return None


@contextmanager
def collapse_excgroups() -> Generator[None, None, None]:
    try:
        yield
    except BaseException as exc:
        if has_exceptiongroups:  # pragma: no cover
            while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
                exc = exc.exceptions[0]

        raise exc


def get_route_path(scope: Scope) -> str:
    path: str = scope["path"]
    root_path = scope.get("root_path", "")
    if not root_path:
        return path

    if not path.startswith(root_path):
        return path

    if path == root_path:
        return ""

    if path[len(root_path)] == "/":
        return path[len(root_path) :]

    return path


_ENTITY_TAG_PATTERN = re.compile(r'\s*(?:(W/)?("[^"]*")|(\*))\s*(?:,|$)')


def parse_http_date(value: str) -> datetime | None:
    """
    Parse an HTTP-date header value, returning None if it cannot be parsed.
    """
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_entity_tags(value: str) -> list[tuple[bool, str]] | None:
    """
    Parse a comma-separated entity-tag list, as used by the If-Match and
    If-None-Match headers.

    Returns a list of (is_weak, tag) tuples, where tag is the quoted
    opaque tag, or "*" for the wildcard. Returns None if the header
    value is malformed, in which case the condition should be ignored.
    """
    tags: list[tuple[bool, str]] = []
    position = 0
    for match in _ENTITY_TAG_PATTERN.finditer(value):
        if match.start() != position:
            return None
        position = match.end()
        if match.group(3) is not None:
            tags.append((False, "*"))
        else:
            tags.append((match.group(1) is not None, match.group(2)))
    if position != len(value):
        return None
    return tags


def _entity_tag_list_matches(tags: list[tuple[bool, str]], etag: str | None, strong: bool) -> bool:
    for is_weak, tag in tags:
        if tag == "*":
            # The wildcard matches any current representation of the resource.
            return True
        if etag is None:
            continue
        current_is_weak = etag.startswith("W/")
        current_tag = etag[2:] if current_is_weak else etag
        if tag != current_tag:
            continue
        # Strong comparison requires both entity-tags to be non-weak.
        if not strong or not (is_weak or current_is_weak):
            return True
    return False


def evaluate_preconditions(
    request_headers: Mapping[str, str],
    response_headers: Mapping[str, str],
    method: str,
) -> int | None:
    """
    Evaluate the If-Match, If-Unmodified-Since, If-None-Match and
    If-Modified-Since request preconditions against the response
    validators, following the precedence order of RFC 9110 section 13.2.2.

    Returns the status code that should be sent instead of the full
    response (412 or 304), or None if the preconditions pass.
    """
    etag = response_headers.get("etag")
    last_modified = None
    if "last-modified" in response_headers:
        last_modified = parse_http_date(response_headers["last-modified"])

    if_match = request_headers.get("if-match")
    if if_match is not None:
        tags = parse_entity_tags(if_match)
        if tags is not None and not _entity_tag_list_matches(tags, etag, strong=True):
            return 412
    else:
        if_unmodified_since = request_headers.get("if-unmodified-since")
        if if_unmodified_since is not None and last_modified is not None:
            unmodified_since = parse_http_date(if_unmodified_since)
            if unmodified_since is not None and last_modified > unmodified_since:
                return 412

    if_none_match = request_headers.get("if-none-match")
    if if_none_match is not None:
        tags = parse_entity_tags(if_none_match)
        if tags is not None and _entity_tag_list_matches(tags, etag, strong=False):
            return 304 if method.upper() in ("GET", "HEAD") else 412
    elif method.upper() in ("GET", "HEAD"):
        if_modified_since = request_headers.get("if-modified-since")
        if if_modified_since is not None and last_modified is not None:
            modified_since = parse_http_date(if_modified_since)
            if modified_since is not None and last_modified <= modified_since:
                return 304

    return None
