"""Minimal Lodestone reader: find a character by name + world and read its Character Profile text.

Lodestone has no official API, so this reads the public HTML pages. That makes it the fragile part of
the character link: if Square Enix changes the markup, only the regexes below need updating
(the parsers are pure functions with tests, so a saved page is enough to fix them).
"""
import asyncio
import html
import re
from dataclasses import dataclass

TIMEOUT_S = 10
USER_AGENT = "raid-matchmaking-bot (character ownership check)"


class LodestoneError(Exception):
    """Lodestone could not be read (down, blocked, markup changed). Message is safe to show players."""


class CharacterNotFound(LodestoneError):
    """No character with that exact name on that world was found."""


@dataclass(frozen=True)
class Profile:
    lodestone_id: str
    name: str
    world: str
    intro: str          # the Character Profile free text ("" if empty)


_TAG = re.compile(r"<[^>]+>")


def _clean(fragment):
    """Strip tags, unescape entities, collapse whitespace."""
    return " ".join(html.unescape(_TAG.sub(" ", fragment)).split())


def _world_only(fragment):
    """'Exodus [Primal]' -> 'Exodus'."""
    m = re.match(r"[A-Za-z]+", _clean(fragment))
    return m.group(0) if m else ""


_ENTRY = re.compile(r'<a\s[^>]*href="/lodestone/character/(\d+)/?"[^>]*>(.*?)</a>', re.S)
_ENTRY_NAME = re.compile(r'class="entry__name"[^>]*>(.*?)</p>', re.S)
_ENTRY_WORLD = re.compile(r'class="entry__world"[^>]*>(.*?)</p>', re.S)

_NAME = re.compile(r'class="frame__chara__name"[^>]*>(.*?)</p>', re.S)
_WORLD = re.compile(r'class="frame__chara__world"[^>]*>(.*?)</p>', re.S)
_INTRO = re.compile(r'class="character__selfintroduction"[^>]*>(.*?)</div>', re.S)


def parse_search(page, name, world):
    """Lodestone id of the search result whose name AND world match exactly (any case), else None.

    The search is a fuzzy "contains" search, so exact matching here is what stops "Ann Tank" from
    being confused with "Ann Tanker".
    """
    for lodestone_id, body in _ENTRY.findall(page):
        n, w = _ENTRY_NAME.search(body), _ENTRY_WORLD.search(body)
        if (n and w and _clean(n.group(1)).lower() == name.lower()
                and _world_only(w.group(1)).lower() == world.lower()):
            return lodestone_id
    return None


def parse_profile(page, lodestone_id):
    n, w = _NAME.search(page), _WORLD.search(page)
    if not n or not w:
        raise LodestoneError("I could not read that Lodestone profile page (is it private?).")
    intro = _INTRO.search(page)
    return Profile(lodestone_id, _clean(n.group(1)), _world_only(w.group(1)),
                   _clean(intro.group(1)) if intro else "")


class LodestoneClient:
    def __init__(self, region="na"):
        self.base = f"https://{region}.finalfantasyxiv.com/lodestone/character/"

    async def _get(self, url, params=None):
        import aiohttp                                  # already installed with discord.py
        try:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
            async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=timeout) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status == 404:
                        raise CharacterNotFound(url)
                    if resp.status != 200:
                        raise LodestoneError(f"Lodestone answered with status {resp.status}.")
                    return await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise LodestoneError("I could not reach Lodestone right now.") from e

    async def fetch_profile(self, name, world):
        """Search for the exact character, then read its profile page.

        Raises CharacterNotFound or LodestoneError.
        """
        page = await self._get(self.base, {"q": name, "worldname": world})
        lodestone_id = parse_search(page, name, world)
        if lodestone_id is None:
            raise CharacterNotFound(f"{name} @ {world}")
        return parse_profile(await self._get(f"{self.base}{lodestone_id}/"), lodestone_id)
