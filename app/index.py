"""Minimal Vercel entrypoint adapter - see docs/VERCEL.md.

Current official Vercel Python support does not auto-detect app/main.py
itself as a function entrypoint, so this one-line re-export is the
smallest adapter that satisfies it. Deliberately does not construct a
second FastAPI application, add any route, or add any middleware - it
only ever re-exports the exact same `app` object every other
environment (local, Docker, CI, tests) already imports from app.main.
"""

from app.main import app

__all__ = ["app"]
