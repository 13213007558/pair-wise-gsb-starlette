from __future__ import annotations

import pytest

from cors_lab.app import (
    TRUSTED_ORIGIN,
    UNTRUSTED_ORIGIN,
    build_inner_app,
    build_outer_app,
    reset_state,
    state,
)
from cors_lab.harness import origin_header, preflight_headers, run


def test_normal_request_inner_gets_reflected_origin_and_credentials() -> None:
    resp = run(build_inner_app(), "GET", "/session", headers=origin_header(TRUSTED_ORIGIN))
    assert resp.status == 200
    assert resp.header("Access-Control-Allow-Origin") == TRUSTED_ORIGIN
    assert resp.header("Access-Control-Allow-Credentials") == "true"
    # SessionMiddleware contributes Vary: Cookie; CORS appends Origin, neither is lost.
    vary = resp.header("Vary")
    assert vary is not None
    assert "Cookie" in vary
    assert "Origin" in vary
    assert resp.set_cookie and "samesite=lax" in resp.set_cookie[0]


def test_existing_vary_is_merged_not_overwritten() -> None:
    resp = run(build_inner_app(), "GET", "/vary", headers=origin_header(TRUSTED_ORIGIN))
    assert resp.header("Vary") == "Accept-Encoding, Origin"


def test_preflight_never_reaches_route() -> None:
    reset_state()
    resp = run(build_inner_app(), "OPTIONS", "/increment", headers=preflight_headers(TRUSTED_ORIGIN))
    assert resp.status == 200
    assert resp.header("Access-Control-Allow-Origin") == TRUSTED_ORIGIN
    assert resp.header("Access-Control-Allow-Credentials") == "true"
    assert state["counter"] == 0  # The route was not called, so no side effect happened.


def test_disallowed_origin_preflight_fails_without_touching_route() -> None:
    reset_state()
    resp = run(build_inner_app(), "OPTIONS", "/increment", headers=preflight_headers(UNTRUSTED_ORIGIN))
    assert resp.status == 400
    assert resp.header("Access-Control-Allow-Origin") is None
    assert state["counter"] == 0


def test_simple_cross_origin_request_executes_even_when_browser_hides_response() -> None:
    reset_state()
    resp = run(build_inner_app(), "POST", "/increment", headers=origin_header(UNTRUSTED_ORIGIN))
    # The server answered 200 and applied the write; only the browser blocks JS access.
    assert resp.status == 200
    assert resp.header("Access-Control-Allow-Origin") is None
    assert state["counter"] == 1


def test_inner_unhandled_exception_loses_cors_headers() -> None:
    # This is the reported bug: CORS is inside ServerErrorMiddleware and never sees the 500.
    resp = run(build_inner_app(), "GET", "/boom", headers=origin_header(TRUSTED_ORIGIN))
    assert resp.status == 500
    assert isinstance(resp.raised, RuntimeError)
    assert resp.header("Access-Control-Allow-Origin") is None
    assert resp.header("Access-Control-Allow-Credentials") is None
    assert resp.header("Vary") is None
    assert resp.set_cookie == []


def test_outer_cors_keeps_headers_on_unhandled_exception() -> None:
    resp = run(build_outer_app(), "GET", "/boom", headers=origin_header(TRUSTED_ORIGIN))
    assert resp.status == 500
    assert isinstance(resp.raised, RuntimeError)  # Exception still propagates for logging.
    assert resp.header("Access-Control-Allow-Origin") == TRUSTED_ORIGIN
    assert resp.header("Access-Control-Allow-Credentials") == "true"
    vary = resp.header("Vary")
    assert vary is not None
    assert "Origin" in vary


def test_side_effect_before_exception_is_committed_in_both_placements() -> None:
    reset_state()
    inner = run(build_inner_app(), "POST", "/count-boom", headers=origin_header(TRUSTED_ORIGIN))
    assert state["counter"] == 1 and inner.header("Access-Control-Allow-Origin") is None

    reset_state()
    outer = run(build_outer_app(), "POST", "/count-boom", headers=origin_header(TRUSTED_ORIGIN))
    assert state["counter"] == 1 and outer.header("Access-Control-Allow-Origin") == TRUSTED_ORIGIN


def test_http_exception_keeps_cors_headers_inner() -> None:
    # HTTPException is converted to a response by ExceptionMiddleware, below CORS in the stack.
    resp = run(build_inner_app(), "GET", "/http-error", headers=origin_header(TRUSTED_ORIGIN))
    assert resp.status == 403
    assert resp.header("Access-Control-Allow-Origin") == TRUSTED_ORIGIN


@pytest.mark.parametrize("origin", [TRUSTED_ORIGIN, UNTRUSTED_ORIGIN])
def test_wildcard_with_credentials_reflects_any_origin(origin: str) -> None:
    app = build_inner_app("wildcard_credentials")
    resp = run(app, "GET", "/session", headers=origin_header(origin))
    # Starlette replaces "*" with the request Origin, so every origin passes credentialed CORS.
    assert resp.header("Access-Control-Allow-Origin") == origin
    assert resp.header("Access-Control-Allow-Credentials") == "true"


def test_wildcard_without_credentials_emits_literal_star() -> None:
    app = build_inner_app("wildcard_no_credentials")
    resp = run(app, "GET", "/session", headers=origin_header(TRUSTED_ORIGIN))
    assert resp.header("Access-Control-Allow-Origin") == "*"
    assert resp.header("Access-Control-Allow-Credentials") is None
