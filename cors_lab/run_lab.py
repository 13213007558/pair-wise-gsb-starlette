from __future__ import annotations

from dataclasses import dataclass

from cors_lab.app import TRUSTED_ORIGIN, UNTRUSTED_ORIGIN, build_inner_app, build_outer_app, reset_state, state
from cors_lab.harness import ASGIResponse, origin_header, preflight_headers, run
from starlette.types import ASGIApp

WATCHED = (
    "Access-Control-Allow-Origin",
    "Access-Control-Allow-Credentials",
    "Vary",
    "Set-Cookie",
)


@dataclass
class Scenario:
    name: str
    placement: str
    preset: str
    method: str
    path: str
    kind: str
    origin: str | None = None
    preflight: bool = False


SCENARIOS = [
    # Same-origin baseline: no Origin header -> CORS middleware stays out of the way.
    Scenario("same-origin GET /session", "inner", "explicit_credentials", "GET", "/session", "normal"),
    # Trusted cross-origin, simple (non-preflighted) requests.
    Scenario("trusted GET /session", "inner", "explicit_credentials", "GET", "/session", "normal", TRUSTED_ORIGIN),
    Scenario(
        "trusted POST /increment", "inner", "explicit_credentials", "POST", "/increment", "normal", TRUSTED_ORIGIN
    ),
    Scenario(
        "trusted GET /vary (app sets Vary)", "inner", "explicit_credentials", "GET", "/vary", "normal", TRUSTED_ORIGIN
    ),
    # Preflight is answered by CORSMiddleware itself; the route is never called.
    Scenario(
        "trusted OPTIONS preflight /increment",
        "inner",
        "explicit_credentials",
        "OPTIONS",
        "/increment",
        "preflight",
        TRUSTED_ORIGIN,
        preflight=True,
    ),
    Scenario(
        "untrusted OPTIONS preflight /increment",
        "inner",
        "explicit_credentials",
        "OPTIONS",
        "/increment",
        "preflight",
        UNTRUSTED_ORIGIN,
        preflight=True,
    ),
    # Untrusted simple request: server executes it, browser just hides the response.
    Scenario(
        "untrusted POST /increment", "inner", "explicit_credentials", "POST", "/increment", "normal", UNTRUSTED_ORIGIN
    ),
    # The failure mode from the ticket: exception routes, both placements.
    Scenario("inner GET /boom (unhandled)", "inner", "explicit_credentials", "GET", "/boom", "error", TRUSTED_ORIGIN),
    Scenario("outer GET /boom (unhandled)", "outer", "explicit_credentials", "GET", "/boom", "error", TRUSTED_ORIGIN),
    Scenario(
        "inner POST /count-boom (write then raise)",
        "inner",
        "explicit_credentials",
        "POST",
        "/count-boom",
        "error",
        TRUSTED_ORIGIN,
    ),
    Scenario(
        "outer POST /count-boom (write then raise)",
        "outer",
        "explicit_credentials",
        "POST",
        "/count-boom",
        "error",
        TRUSTED_ORIGIN,
    ),
    Scenario(
        "inner GET /http-error (HTTPException)",
        "inner",
        "explicit_credentials",
        "GET",
        "/http-error",
        "error",
        TRUSTED_ORIGIN,
    ),
    Scenario(
        "outer GET /http-error (HTTPException)",
        "outer",
        "explicit_credentials",
        "GET",
        "/http-error",
        "error",
        TRUSTED_ORIGIN,
    ),
    # Wildcard presets, credentials on/off.
    Scenario(
        "wildcard* no-credentials GET /session",
        "inner",
        "wildcard_no_credentials",
        "GET",
        "/session",
        "normal",
        TRUSTED_ORIGIN,
    ),
    Scenario(
        "wildcard* WITH credentials GET /session",
        "inner",
        "wildcard_credentials",
        "GET",
        "/session",
        "normal",
        TRUSTED_ORIGIN,
    ),
    Scenario(
        "wildcard* no-credentials GET /boom",
        "inner",
        "wildcard_no_credentials",
        "GET",
        "/boom",
        "error",
        TRUSTED_ORIGIN,
    ),
    Scenario(
        "outer wildcard* WITH credentials GET /boom",
        "outer",
        "wildcard_credentials",
        "GET",
        "/boom",
        "error",
        TRUSTED_ORIGIN,
    ),
    # "*" + credentials reflects EVERY origin, including the attacker's.
    Scenario(
        "wildcard* credentials, UNTRUSTED preflight",
        "inner",
        "wildcard_credentials",
        "OPTIONS",
        "/increment",
        "preflight",
        UNTRUSTED_ORIGIN,
        preflight=True,
    ),
    Scenario(
        "wildcard* credentials, UNTRUSTED POST /increment",
        "inner",
        "wildcard_credentials",
        "POST",
        "/increment",
        "normal",
        UNTRUSTED_ORIGIN,
    ),
]


def build(placement: str, preset: str) -> ASGIApp:
    return build_outer_app(preset) if placement == "outer" else build_inner_app(preset)


def headers_for(scn: Scenario) -> list[tuple[bytes, bytes]]:
    if scn.preflight:
        return preflight_headers(scn.origin or TRUSTED_ORIGIN)
    headers = origin_header(scn.origin) if scn.origin else []
    if scn.method == "POST":
        headers.append((b"content-type", b"application/json"))
    return headers


def watched_values(resp: ASGIResponse) -> dict[str, str]:
    out = {}
    for name in WATCHED:
        values = resp.getlist(name)
        if name == "Set-Cookie":
            out[name] = f"{len(values)} cookie(s)" if values else "-"
        else:
            out[name] = values[0] if values else "-"
    return out


def browser_verdict(scn: Scenario, resp: ASGIResponse) -> str:
    # Derived from the fetch/CORS rules, not from whether the ASGI caller can read the body.
    if scn.kind == "preflight":
        if resp.header("Access-Control-Allow-Origin") is None:
            return "preflight FAIL -> real request never sent"
        return "preflight OK -> browser then sends the real request"
    if scn.origin is None:
        return "same-origin: no CORS check"
    allow_origin = resp.header("Access-Control-Allow-Origin")
    credentials = resp.header("Access-Control-Allow-Credentials")
    if allow_origin is None:
        return "CORS FAIL -> response hidden from JS"
    if credentials == "true" and allow_origin == "*":
        return "invalid for credentialed fetch (wildcard + credentials)"
    return "CORS OK -> JS may read the response"


def side_effect_note(scn: Scenario, before: int) -> str:
    after = state["counter"]
    if scn.preflight:
        return "no route call; counter unchanged"
    if after > before:
        return f"side effect ALREADY APPLIED (counter {before} -> {after})"
    return f"no counter change ({after})"


def main() -> None:
    rows = []
    for scn in SCENARIOS:
        reset_state()
        app = build(scn.placement, scn.preset)
        before = state["counter"]
        resp = run(app, scn.method, scn.path, headers=headers_for(scn))
        values = watched_values(resp)
        rows.append(
            {
                "scenario": scn.name,
                "status": resp.status,
                "raised": type(resp.raised).__name__ if resp.raised else "-",
                **values,
                "browser": browser_verdict(scn, resp),
                "effect": side_effect_note(scn, before),
            }
        )

    headers = ["scenario", "status", "raised"] + list(WATCHED) + ["browser", "effect"]
    widths = {h: max(len(h), *(len(str(r[h])) for r in rows)) for h in headers}

    def line(row: dict[str, object]) -> str:
        return " | ".join(str(row[h]).ljust(widths[h]) for h in headers)

    print("=" * 120)
    print("In-memory ASGI comparison (Starlette 1.10.0). The ASGI caller always sees the raw response;")
    print("the 'browser' column is derived from fetch/CORS rules separately.")
    print("=" * 120)
    print(line(dict(zip(headers, headers))))
    print("-+-".join("-" * widths[h] for h in headers))
    for row in rows:
        print(line(row))

    print("\nDetailed Set-Cookie attributes where a cookie was issued:")
    for scn in SCENARIOS:
        reset_state()
        resp = run(build(scn.placement, scn.preset), scn.method, scn.path, headers=headers_for(scn))
        for cookie in resp.set_cookie:
            print(f"  {scn.name}\n    {cookie}")


if __name__ == "__main__":
    main()
