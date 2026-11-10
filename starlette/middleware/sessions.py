from __future__ import annotations

import json
import secrets
import threading
import typing
from base64 import b64decode, b64encode
from typing import Literal

import itsdangerous
from itsdangerous.exc import BadSignature

from starlette.datastructures import MutableHeaders, Secret
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send


@typing.runtime_checkable
class SessionRevocationStore(typing.Protocol):
    """
    Server-side record of revoked session identities.

    Both methods receive the signature-protected `session_id` and `generation`
    embedded in the session cookie. `revoke()` must invalidate the given
    generation and every generation before it, so that cookies issued earlier
    are rejected even if the browser keeps presenting them.

    Implementations must be safe to call concurrently. The in-memory adapter
    below covers single-process applications; adapters backed by external
    storage can add persistence and multi-process sharing.
    """

    async def is_revoked(self, session_id: str, generation: int) -> bool: ...  # pragma: no cover

    async def revoke(self, session_id: str, generation: int) -> None: ...  # pragma: no cover


class InMemoryRevocationStore:
    """
    Thread-safe, process-local `SessionRevocationStore`.

    Revocations are kept in memory, so they are lost on restart and are not
    shared between processes. The mapping grows by one entry per revoked
    session id; applications that revoke very large numbers of sessions
    should use an adapter with eviction or persistence instead.
    """

    def __init__(self) -> None:
        self._revoked_generations: dict[str, int] = {}
        self._lock = threading.Lock()

    async def is_revoked(self, session_id: str, generation: int) -> bool:
        with self._lock:
            revoked = self._revoked_generations.get(session_id)
        return revoked is not None and generation <= revoked

    async def revoke(self, session_id: str, generation: int) -> None:
        with self._lock:
            current = self._revoked_generations.get(session_id)
            if current is None or generation > current:
                self._revoked_generations[session_id] = generation


class SessionMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        secret_key: str | Secret,
        session_cookie: str = "session",
        max_age: int | None = 14 * 24 * 60 * 60,  # 14 days, in seconds
        path: str = "/",
        same_site: Literal["lax", "strict", "none"] = "lax",
        https_only: bool = False,
        domain: str | None = None,
        partitioned: bool = False,
        revocation_store: SessionRevocationStore | None = None,
    ) -> None:
        self.app = app
        self.signer = itsdangerous.TimestampSigner(str(secret_key))
        self.session_cookie = session_cookie
        self.max_age = max_age
        self.path = path
        self.revocation_store = revocation_store
        self.security_flags = "httponly; samesite=" + same_site
        if https_only:  # Secure flag can be used with HTTPS only
            self.security_flags += "; secure"
        if domain is not None:
            self.security_flags += f"; domain={domain}"
        if partitioned:
            self.security_flags += "; partitioned"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):  # pragma: no cover
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        initial_session_was_empty = True

        if self.session_cookie in connection.cookies:
            data = connection.cookies[self.session_cookie].encode("utf-8")
            try:
                data = self.signer.unsign(data, max_age=self.max_age)
                if self.revocation_store is not None:
                    scope["session"] = await self._load_revocable(data)
                    initial_session_was_empty = scope["session"]._id is None
                else:
                    scope["session"] = Session(json.loads(b64decode(data)))
                    initial_session_was_empty = False
            except BadSignature:
                scope["session"] = Session()
        else:
            scope["session"] = Session()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                session: Session = scope["session"]
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                if session.accessed:
                    headers.add_vary_header("Cookie")
                if self.revocation_store is not None:
                    await self._send_revocable(session, headers, initial_session_was_empty)
                elif session.modified and session:
                    # We have session data to persist.
                    data = b64encode(json.dumps(session).encode("utf-8"))
                    data = self.signer.sign(data)
                    header_value = "{session_cookie}={data}; path={path}; {max_age}{security_flags}".format(
                        session_cookie=self.session_cookie,
                        data=data.decode("utf-8"),
                        path=self.path,
                        max_age=f"Max-Age={self.max_age}; " if self.max_age is not None else "",
                        security_flags=self.security_flags,
                    )
                    headers.append("Set-Cookie", header_value)
                elif session.modified and not initial_session_was_empty:
                    # The session has been cleared.
                    headers.append("Set-Cookie", self._clear_cookie_header())
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _load_revocable(self, data: bytes) -> Session:
        """
        Unpack a signed `{"s": session_id, "g": generation, "d": data}` payload
        and reject identities the store has revoked.

        Revocation checks fail closed: if the store cannot be queried, the
        cookie is treated as revoked rather than trusted.
        """
        try:
            payload = json.loads(b64decode(data))
            session_id = payload["s"]
            generation = payload["g"]
            session_data = payload["d"]
        except (TypeError, KeyError, ValueError):
            return Session()
        try:
            revoked = await self.revocation_store.is_revoked(session_id, generation)  # type: ignore[union-attr]
        except Exception:
            revoked = True
        if revoked:
            return Session()
        return Session(session_data, _id=session_id, _generation=generation)

    async def _send_revocable(self, session: Session, headers: MutableHeaders, initial_session_was_empty: bool) -> None:
        """
        Write the session cookie for revocation-aware mode.

        This runs when `http.response.start` is about to be sent, and applies
        the same contract as loading and explicit logout: an identity that has
        been revoked must not be written back as a valid session. Once
        `http.response.start` has been sent the headers are on the wire and
        this decision can no longer be revisited.
        """
        session_id, generation = session._id, session._generation
        if session._revoke_requested:
            if session_id is not None:
                await self.revocation_store.revoke(session_id, generation)  # type: ignore[union-attr]
            session_id = session._id = None
        if session.modified and session:
            # We have session data to persist.
            if session_id is None:
                # No usable identity (fresh login, or the old one was revoked
                # or rejected on load): issue an independent one.
                session_id, generation = secrets.token_urlsafe(32), 0
                session._id, session._generation = session_id, generation
            else:
                try:
                    revoked = await self.revocation_store.is_revoked(session_id, generation)  # type: ignore[union-attr]
                except Exception:
                    revoked = True
                if revoked:
                    # Logout (or a revocation check failure) took effect after
                    # this request loaded the session. Do not write the old
                    # identity back; expire the cookie instead.
                    headers.append("Set-Cookie", self._clear_cookie_header())
                    return
            data = b64encode(json.dumps({"s": session_id, "g": generation, "d": session}).encode("utf-8"))
            data = self.signer.sign(data)
            header_value = "{session_cookie}={data}; path={path}; {max_age}{security_flags}".format(
                session_cookie=self.session_cookie,
                data=data.decode("utf-8"),
                path=self.path,
                max_age=f"Max-Age={self.max_age}; " if self.max_age is not None else "",
                security_flags=self.security_flags,
            )
            headers.append("Set-Cookie", header_value)
        elif session.modified and (not initial_session_was_empty or session._revoke_requested):
            # The session has been cleared or explicitly revoked.
            headers.append("Set-Cookie", self._clear_cookie_header())

    def _clear_cookie_header(self) -> str:
        return "{session_cookie}={data}; path={path}; {expires}{security_flags}".format(
            session_cookie=self.session_cookie,
            data="null",
            path=self.path,
            expires="expires=Thu, 01 Jan 1970 00:00:00 GMT; ",
            security_flags=self.security_flags,
        )


class Session(dict[str, typing.Any]):
    accessed: bool = False
    modified: bool = False
    _revoke_requested: bool = False

    def __init__(self, *args: typing.Any, _id: str | None = None, _generation: int = 0, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        self._id = _id
        self._generation = _generation

    def revoke(self) -> None:
        """
        Explicitly log out: clear the session data and, when the middleware is
        configured with a `revocation_store`, record the session identity as
        revoked server-side so the old cookie is rejected from then on.
        """
        self._revoke_requested = True
        self.clear()

    def mark_accessed(self) -> None:
        self.accessed = True

    def mark_modified(self) -> None:
        self.accessed = True
        self.modified = True

    def __setitem__(self, key: str, value: typing.Any) -> None:
        self.mark_modified()
        super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        self.mark_modified()
        super().__delitem__(key)

    def clear(self) -> None:
        self.mark_modified()
        super().clear()

    def pop(self, key: str, *args: typing.Any) -> typing.Any:
        if key in self:
            self.mark_modified()
        return super().pop(key, *args)

    def popitem(self) -> tuple[str, typing.Any]:
        item = super().popitem()
        self.mark_modified()
        return item

    def setdefault(self, key: str, default: typing.Any = None) -> typing.Any:
        if key not in self:
            self.mark_modified()
        return super().setdefault(key, default)

    def update(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        self.mark_modified()
        super().update(*args, **kwargs)

    def __ior__(self, other: typing.Any, /) -> Session:  # type: ignore[override,misc]
        self.mark_modified()
        return super().__ior__(other)
