from __future__ import annotations

from collections.abc import Sequence
from ipaddress import AddressValueError, IPv6Address

from starlette._utils import ParsedHost, parse_host_header
from starlette.datastructures import URL, Headers
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

ENFORCE_DOMAIN_WILDCARD = "Domain wildcard patterns must be like '*.example.com'."
ENFORCE_VALID_ALLOWED_HOST = "Allowed host patterns must be valid authorities, e.g. 'example.com:8080'."

DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def normalize_port(port: str | None, default_port: int) -> int:
    if port is None:
        return default_port
    return int(port.lstrip("0") or "0")


def parse_strict_authority(scope: Scope) -> ParsedHost | None:
    """Extract the authority from the raw ASGI headers, applying strict validation.

    Returns `None` unless there is exactly one `Host` header, it contains no
    control characters, its port is valid, and any bracketed literal is a
    genuine IPv6 address rather than an ambiguous IPvFuture form.
    """
    host_values = [value for key, value in scope["headers"] if key == b"host"]
    if len(host_values) != 1:
        return None
    host_header = host_values[0].decode("latin-1")
    if any(ord(char) < 0x21 or 0x7F <= ord(char) <= 0x9F for char in host_header):
        return None
    parsed = parse_host_header(host_header)
    if parsed is None or not parsed.is_valid_port:
        return None
    if parsed.host.startswith("["):
        try:
            IPv6Address(parsed.host[1:-1])
        except AddressValueError:
            return None
    return parsed


class TrustedHostMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: Sequence[str] | None = None,
        www_redirect: bool = True,
        strict: bool = False,
    ) -> None:
        if allowed_hosts is None:
            allowed_hosts = ["*"]

        for pattern in allowed_hosts:
            assert "*" not in pattern[1:], ENFORCE_DOMAIN_WILDCARD
            if pattern.startswith("*") and pattern != "*":
                assert pattern.startswith("*."), ENFORCE_DOMAIN_WILDCARD
        self.app = app
        self.allowed_hosts = [pattern.lower() for pattern in allowed_hosts]
        self.allow_any = "*" in allowed_hosts
        self.www_redirect = www_redirect
        self.strict = strict
        self.allowed_host_patterns: list[tuple[str, str | None]] = []
        if strict:
            for pattern in self.allowed_hosts:
                parsed_pattern = parse_host_header(pattern)
                assert parsed_pattern is not None and parsed_pattern.is_valid_port, ENFORCE_VALID_ALLOWED_HOST
                self.allowed_host_patterns.append((parsed_pattern.host, parsed_pattern.port))

    def matches_allowed_host(self, parsed_host: ParsedHost, scope: Scope) -> tuple[bool, bool]:
        host = parsed_host.host.lower()
        if not self.strict:
            is_valid_host = False
            found_www_redirect = False
            for pattern in self.allowed_hosts:
                if host == pattern or (pattern.startswith("*") and host.endswith(pattern[1:])):
                    is_valid_host = True
                    break
                elif "www." + host == pattern:
                    found_www_redirect = True
            return is_valid_host, found_www_redirect

        default_port = DEFAULT_PORTS.get(scope.get("scheme", "http"), 80)
        request_port = normalize_port(parsed_host.port, default_port)
        is_valid_host = False
        found_www_redirect = False
        for pattern_host, pattern_port in self.allowed_host_patterns:
            if host == pattern_host or (pattern_host.startswith("*") and host.endswith(pattern_host[1:])):
                if request_port == normalize_port(pattern_port, default_port):
                    is_valid_host = True
                    break
            elif "www." + host == pattern_host:
                found_www_redirect = True
        return is_valid_host, found_www_redirect

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.allow_any or scope["type"] not in (
            "http",
            "websocket",
        ):  # pragma: no cover
            await self.app(scope, receive, send)
            return

        if self.strict:
            parsed_host = parse_strict_authority(scope)
        else:
            headers = Headers(scope=scope)
            parsed_host = parse_host_header(headers.get("host"))
        if parsed_host is None:
            await PlainTextResponse("Invalid host header", status_code=400)(scope, receive, send)
            return

        is_valid_host, found_www_redirect = self.matches_allowed_host(parsed_host, scope)

        if is_valid_host:
            await self.app(scope, receive, send)
        else:
            response: Response
            if found_www_redirect and self.www_redirect:
                url = URL(scope=scope)
                # In strict mode the redirect is built from the validated
                # authority, never from the raw request headers.
                netloc = "www." + parsed_host.authority if self.strict else "www." + url.netloc
                redirect_url = url.replace(netloc=netloc)
                response = RedirectResponse(url=str(redirect_url))
            else:
                response = PlainTextResponse("Invalid host header", status_code=400)
            await response(scope, receive, send)
