from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp

SECRET_KEY = "lab-secret-not-for-production"
TRUSTED_ORIGIN = "https://app.example"
UNTRUSTED_ORIGIN = "https://evil.example"

# In-memory side-effect state. reset_state() is called between scenarios.
state = {"counter": 0}


def reset_state() -> None:
    state["counter"] = 0


async def session_view(request: Request) -> JSONResponse:
    # Reading the session marks it accessed -> SessionMiddleware adds Vary: Cookie.
    views = request.session.get("views", 0)
    request.session["views"] = views + 1
    return JSONResponse({"views": request.session["views"], "counter": state["counter"]})


async def increment(request: Request) -> JSONResponse:
    state["counter"] += 1
    return JSONResponse({"counter": state["counter"]})


async def increment_then_boom(request: Request) -> JSONResponse:
    # The write happens first; the exception is raised afterwards.
    state["counter"] += 1
    raise RuntimeError("boom after counter increment")


async def boom(request: Request) -> JSONResponse:
    raise RuntimeError("intentional unhandled error")


async def http_error(request: Request) -> JSONResponse:
    raise HTTPException(status_code=403, detail="forbidden by policy")


async def vary_view(request: Request) -> JSONResponse:
    # The application contributes its own Vary header on purpose.
    return JSONResponse({"ok": True}, headers={"Vary": "Accept-Encoding"})


routes = [
    Route("/session", session_view, methods=["GET"]),
    Route("/increment", increment, methods=["POST"]),
    Route("/count-boom", increment_then_boom, methods=["POST"]),
    Route("/boom", boom, methods=["GET"]),
    Route("/http-error", http_error, methods=["GET"]),
    Route("/vary", vary_view, methods=["GET"]),
]


def cors_kwargs(preset: str) -> dict[str, Any]:
    common: dict[str, Any] = {
        "allow_methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "X-CSRF-Token"],
    }
    if preset == "explicit_credentials":
        # Recommended: explicit origin list + credentials.
        return {**common, "allow_origins": [TRUSTED_ORIGIN], "allow_credentials": True}
    if preset == "wildcard_no_credentials":
        return {**common, "allow_origins": ["*"], "allow_credentials": False}
    if preset == "wildcard_credentials":
        # Insecure on purpose: Starlette silently turns "*" into origin reflection.
        return {**common, "allow_origins": ["*"], "allow_credentials": True}
    raise KeyError(preset)


def build_inner_app(preset: str = "explicit_credentials") -> Starlette:
    # CORSMiddleware lives INSIDE the application, i.e. inside ServerErrorMiddleware.
    # This is what app.add_middleware(CORSMiddleware, ...) produces.
    return Starlette(
        routes=routes,
        middleware=[
            Middleware(CORSMiddleware, **cors_kwargs(preset)),
            Middleware(SessionMiddleware, secret_key=SECRET_KEY),
        ],
    )


def build_outer_app(preset: str = "explicit_credentials") -> ASGIApp:
    # CORSMiddleware wraps the whole application, sitting OUTSIDE ServerErrorMiddleware.
    inner = Starlette(
        routes=routes,
        middleware=[Middleware(SessionMiddleware, secret_key=SECRET_KEY)],
    )
    return CORSMiddleware(inner, **cors_kwargs(preset))
