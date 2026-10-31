"""Minimal FFLogs v2 client: find a character by Lodestone id or FFLogs id.

Auth is OAuth client credentials (create a client at fflogs.com/api/clients; keep the secret out of git).
FFLogs only knows characters that have logs or rankings, so `identity` returns None for the others.
"""
import asyncio
import time
from dataclasses import dataclass

TOKEN_URL = "https://www.fflogs.com/oauth/token"
API_URL = "https://www.fflogs.com/api/v2/client"
TIMEOUT_S = 10

_FIELDS = "id canonicalID name lodestoneID server { name }"
BY_LODESTONE = "query($i: Int!) { characterData { character(lodestoneID: $i) { %s } } }" % _FIELDS
BY_ID = "query($i: Int!) { characterData { character(id: $i) { %s } } }" % _FIELDS


class FFLogsError(Exception):
    """FFLogs could not be read (down, bad credentials, schema changed)."""


@dataclass(frozen=True)
class LogsIdentity:
    fflogs_id: int       # canonical id: follows renames and transfers
    lodestone_id: str
    name: str
    world: str


def parse_identity(data):
    """GraphQL `data` -> (LogsIdentity, canonical id that differs from this record's id or None)."""
    char = ((data or {}).get("characterData") or {}).get("character")
    if not char:
        return None, None
    ident = LogsIdentity(int(char["canonicalID"] or char["id"]), str(char["lodestoneID"]),
                         char["name"], char["server"]["name"])
    return ident, (ident.fflogs_id if ident.fflogs_id != char["id"] else None)


class FFLogsClient:
    def __init__(self, client_id, client_secret):
        self.client_id, self.client_secret = client_id, client_secret
        self._token, self._expires = None, 0.0

    async def _post(self, session, url, **kw):
        async with session.post(url, **kw) as resp:
            if resp.status != 200:
                raise FFLogsError(f"FFLogs answered with status {resp.status}.")
            return await resp.json()

    async def _query(self, query, variables):
        import aiohttp
        try:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if not self._token or time.time() > self._expires:
                    tok = await self._post(session, TOKEN_URL, data={"grant_type": "client_credentials"},
                                           auth=aiohttp.BasicAuth(self.client_id, self.client_secret))
                    self._token, self._expires = tok["access_token"], time.time() + tok.get("expires_in", 300) - 60
                body = await self._post(session, API_URL, json={"query": query, "variables": variables},
                                        headers={"Authorization": f"Bearer {self._token}"})
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError) as e:
            raise FFLogsError("I could not reach FFLogs right now.") from e
        if body.get("errors"):
            raise FFLogsError("FFLogs rejected the request.")
        return body.get("data")

    async def identity(self, lodestone_id=None, fflogs_id=None):
        """LogsIdentity of the character's most recent name/server, or None if FFLogs does not know it."""
        query, key = (BY_ID, fflogs_id) if fflogs_id else (BY_LODESTONE, int(lodestone_id))
        ident, newer = parse_identity(await self._query(query, {"i": key}))
        if newer:                                   # renamed or transferred: ask for the current record
            ident, _ = parse_identity(await self._query(BY_ID, {"i": newer}))
        return ident
