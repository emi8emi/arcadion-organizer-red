from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, runs
from .db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET,
    https_only=config.COOKIE_SECURE,
    same_site="lax",
    max_age=60 * 60 * 24 * 7,  # stay logged in for 7 days
)

app.include_router(auth.router)
app.include_router(runs.router)

# In production the built Svelte app (web/build) is served by this same server.
# In dev the folder doesn't exist yet, so this is skipped.
WEB_BUILD = Path(__file__).resolve().parent.parent.parent / "web" / "build"
if WEB_BUILD.exists():
    app.mount("/", StaticFiles(directory=WEB_BUILD, html=True), name="web")
