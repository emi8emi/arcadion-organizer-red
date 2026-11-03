import secrets
import urllib.parse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import config

router = APIRouter(prefix="/api/auth")

DISCORD_API = "https://discord.com/api/v10"
REDIRECT_URI = f"{config.BASE_URL}/api/auth/callback"


def current_user(request: Request) -> dict:
    """Use as a dependency on any route that only organizers may call."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user


@router.get("/login")
def login(request: Request):
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    params = {
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    url = "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(params)
    return RedirectResponse(url)


@router.get("/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None):
    expected = request.session.pop("oauth_state", None)
    if not code or not state or state != expected:
        raise HTTPException(status_code=400, detail="Invalid login state, try again")

    async with httpx.AsyncClient(timeout=10) as client:
        # 1) swap the one-time code for the user's access token
        token_res = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": config.DISCORD_CLIENT_ID,
                "client_secret": config.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
        )
        if token_res.status_code != 200:
            raise HTTPException(status_code=400, detail="Discord rejected the login")
        access_token = token_res.json()["access_token"]

        # 2) who is this?
        me_res = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if me_res.status_code != 200:
            raise HTTPException(status_code=400, detail="Could not read your Discord profile")
        me = me_res.json()

        # 3) ask Discord (as the bot) what roles they have in your server
        member_res = await client.get(
            f"{DISCORD_API}/guilds/{config.DISCORD_GUILD_ID}/members/{me['id']}",
            headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
        )

    if member_res.status_code == 404:
        return RedirectResponse(f"{config.BASE_URL}/?error=not_in_server")
    if member_res.status_code != 200:
        return RedirectResponse(f"{config.BASE_URL}/?error=discord_error")

    roles = member_res.json().get("roles", [])
    if config.ORGANIZER_ROLE_ID not in roles:
        return RedirectResponse(f"{config.BASE_URL}/?error=not_organizer")

    request.session["user"] = {
        "id": me["id"],
        "name": me.get("global_name") or me["username"],
    }
    return RedirectResponse(f"{config.BASE_URL}/")


@router.get("/me")
def me(request: Request):
    return current_user(request)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}
