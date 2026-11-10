# CORS placement lab (Starlette 1.10.0)

This lab reproduces the "JSON works, but raised exceptions only show a CORS error"
behaviour using in-memory ASGI calls. No network port and no browser are involved;
the browser-side conclusions in the report are derived from the Fetch/CORS and
cookie rules, not from what the test caller can read.

## How to rerun

Both commands run fully in-process against the ASGI app, with no port binding or browser:

```bash
# Comparison table (headers, browser verdict, side effects)
.venv/bin/python -m cors_lab.run_lab

# Pinned assertions for every conclusion
.venv/bin/python -m pytest cors_lab/ -q
```

## Files

- `app.py` — three routes (`GET /session`, `POST /increment`, error routes) plus the
  same application built with `CORSMiddleware` **inside** the app and **wrapping** it.
- `harness.py` — raw ASGI caller that captures the exact response-start headers and
  records any exception re-raised after a 500 response was already sent.
- `run_lab.py` — the scenario matrix and the printed comparison.
- `test_cors_lab.py` — pytest assertions backing each claim in `REPORT.md`.

The ASGI caller sees every response, including 500s without CORS headers. That is a
property of the caller, not of a browser: a browser running the Fetch algorithm hides
such responses from JavaScript and reports a CORS error instead.
