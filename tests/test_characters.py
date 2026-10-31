"""Characters: claim + Lodestone verification (fake Lodestone), alts, takeover, FFLogs sync, migration, parsers."""
import sqlite3
from datetime import timedelta

import pytest

from bot import character_handlers as h
from bot import characters as ch
from bot.fflogs import FFLogsError, LogsIdentity, parse_identity
from bot.lodestone import CharacterNotFound, LodestoneError, Profile, parse_profile, parse_search
from conftest import NOW, player, run_async


class FakeLodestone:
    def __init__(self, *profiles, error=None):
        self.profiles = {(p.name.lower(), p.world.lower()): p for p in profiles}
        self.error, self.calls = error, 0

    async def fetch_profile(self, name, world):
        self.calls += 1
        if self.error:
            raise self.error
        p = self.profiles.get((name.lower(), world.lower()))
        if p is None:
            raise CharacterNotFound(name)
        return p


class FakeFFLogs:
    def __init__(self, *idents, error=None):
        self.by_lodestone = {i.lodestone_id: i for i in idents}
        self.by_id = {i.fflogs_id: i for i in idents}
        self.error, self.calls = error, []

    async def identity(self, lodestone_id=None, fflogs_id=None):
        self.calls.append((lodestone_id, fflogs_id))
        if self.error:
            raise self.error
        return self.by_id.get(fflogs_id) if fflogs_id else self.by_lodestone.get(lodestone_id)


def profile(name="Ann Tank", world="Exodus", intro="", lid="111"):
    return Profile(lid, name, world, intro)


def do(ctx, handler, it, *args):
    run_async(handler(ctx, it, *args))
    return it


def add(ctx, uid, name="Ann Tank", world="Exodus"):
    return do(ctx, h.character_add, player(uid, f"P{uid}"), name, world)


def code(ctx, uid):
    return ch.get_claim(ctx.db, uid)["token"]


def verify(ctx, uid, clock=None):
    if clock:
        clock["now"] += timedelta(seconds=31)      # step over the per-player cooldown
    return do(ctx, h.character_verify, player(uid))


def verified(ctx, clock, uid, lodestone_profile_args=None, **kw):
    """Run add + verify for a character whose profile carries the code."""
    p = profile(**kw)
    ctx.lodestone = FakeLodestone(p)
    add(ctx, uid, p.name, p.world)
    ctx.lodestone = FakeLodestone(profile(**{**kw, "intro": code(ctx, uid)}))
    return verify(ctx, uid, clock)


# ---------------------------------------------------------------- add

def test_add_stores_only_a_claim_and_hands_out_a_code(ctx):
    it = add(ctx, 5, "  ann   TANK ", "exodus")
    claim = ch.get_claim(ctx.db, 5)
    assert (claim["name"], claim["world"]) == ("Ann Tank", "Exodus")
    assert claim["token"].startswith("raidbot-") and claim["token"] in it.text
    assert ch.list_characters(ctx.db, 5) == []          # nothing is a character until verified
    assert it.response.sent[0]["ephemeral"] is True


@pytest.mark.parametrize("name, world", [("Ann", "Exodus"), ("Ann T4nk", "Exodus"), ("Ann Tank Jr", "Exodus"),
                                         ("Ann Tank", "Exo dus"), ("Ann Tank", ""), ("", "Exodus")])
def test_add_rejects_nonsense(ctx, name, world):
    it = add(ctx, 5, name, world)
    assert "name" in it.text or "world" in it.text
    assert ch.get_claim(ctx.db, 5) is None


def test_only_one_pending_claim_per_player(ctx):
    add(ctx, 5, "Ann Tank")
    first = code(ctx, 5)
    it = add(ctx, 5, "Bob Healer")
    claim = ch.get_claim(ctx.db, 5)
    assert (claim["name"], claim["token"] != first) == ("Bob Healer", True)
    assert "replaced" in it.text
    assert ctx.db.conn.execute("SELECT COUNT(*) FROM character_claims").fetchone()[0] == 1


# ---------------------------------------------------------------- verify

def test_verify_needs_a_claim_and_lodestone(ctx):
    ctx.lodestone = FakeLodestone()
    assert "Nothing to verify" in verify(ctx, 5).text
    ctx.lodestone = None
    add(ctx, 5)
    assert "not set up" in verify(ctx, 5).text


def test_verify_full_flow_and_canonical_spelling(ctx, clock):
    ctx.lodestone = FakeLodestone(profile(name="ANN Tank", intro="just a profile"))
    add(ctx, 5, "ann tank")
    miss = verify(ctx, 5, clock)
    assert "not your code" in miss.text and ch.list_characters(ctx.db, 5) == []
    token = code(ctx, 5)
    ctx.lodestone = FakeLodestone(profile(name="ANN Tank", intro=f"hello {token.upper()} bye"))
    ok = verify(ctx, 5, clock)
    [c] = ch.list_characters(ctx.db, 5)
    assert "Verified" in ok.text and (c["name"], c["lodestone_id"]) == ("ANN Tank", "111")
    assert ch.get_claim(ctx.db, 5) is None


def test_verify_is_rate_limited(ctx):
    ctx.lodestone = FakeLodestone(profile())
    add(ctx, 5)
    do(ctx, h.character_verify, player(5))
    it = do(ctx, h.character_verify, player(5))
    assert "Please wait" in it.text and ctx.lodestone.calls == 1


def test_an_old_code_is_replaced_after_an_hour(ctx, clock):
    ctx.lodestone = FakeLodestone(profile())
    add(ctx, 5)
    old = code(ctx, 5)
    clock["now"] = NOW + timedelta(hours=2)
    it = do(ctx, h.character_verify, player(5))
    assert code(ctx, 5) != old and code(ctx, 5) in it.text and ctx.lodestone.calls == 0


def test_lodestone_trouble_keeps_the_code(ctx, clock):
    add(ctx, 5)
    token = code(ctx, 5)
    ctx.lodestone = FakeLodestone(error=LodestoneError("I could not reach Lodestone right now."))
    assert "could not reach Lodestone" in verify(ctx, 5, clock).text
    ctx.lodestone = FakeLodestone()
    assert "no character called Ann Tank" in verify(ctx, 5, clock).text
    assert code(ctx, 5) == token


# ---------------------------------------------------------------- alts, takeover, list/remove

def test_a_player_can_verify_several_characters(ctx, clock):
    verified(ctx, clock, 5, name="Ann Tank", lid="111")
    verified(ctx, clock, 5, name="Ann Alt", lid="112")
    assert [c["name"] for c in ch.list_characters(ctx.db, 5)] == ["Ann Tank", "Ann Alt"]
    assert "Ann Tank @ Exodus, Ann Alt @ Exodus" in do(ctx, h.character_list, player(5)).text


def test_character_limit_and_duplicates(ctx, clock, monkeypatch):
    verified(ctx, clock, 5, name="Ann Tank", lid="111")
    assert "already one of your verified" in add(ctx, 5, "ann tank").text
    monkeypatch.setattr(ch, "MAX_CHARACTERS", 1)
    assert "up to 1" in add(ctx, 5, "Bob Healer").text


def test_verifying_takes_a_character_over_silently(ctx, clock):
    verified(ctx, clock, 1, lid="111")
    verified_again = verified(ctx, clock, 2, lid="111")
    assert "Someone else had verified" in verified_again.text
    assert ch.list_characters(ctx.db, 1) == []
    assert [c["lodestone_id"] for c in ch.list_characters(ctx.db, 2)] == ["111"]


def test_verifying_a_renamed_character_updates_the_row_instead_of_duplicating(ctx, clock):
    verified(ctx, clock, 5, name="Ann Tank", lid="111")
    verified(ctx, clock, 5, name="Ann Renamed", lid="111")        # same Lodestone id under a new name
    [c] = ch.list_characters(ctx.db, 5)
    assert c["name"] == "Ann Renamed"


def test_list_and_remove_only_your_own(ctx, clock):
    assert "none" in do(ctx, h.character_list, player(5)).text
    verified(ctx, clock, 1)
    [c] = ch.list_characters(ctx.db, 1)
    assert "no such character" in do(ctx, h.character_remove, player(2), c["id"]).text
    assert len(ch.list_characters(ctx.db, 1)) == 1
    assert "Removed" in do(ctx, h.character_remove, player(1), c["id"]).text
    assert ch.list_characters(ctx.db, 1) == []


def test_choices_for_autocomplete(ctx, clock):
    verified(ctx, clock, 5)
    assert h.character_choices(ctx, 5) == [("Ann Tank @ Exodus", ch.list_characters(ctx.db, 5)[0]["id"])]


# ---------------------------------------------------------------- FFLogs sync

def test_fflogs_id_is_stored_on_verify_and_refresh_follows_renames(ctx, clock):
    ctx.fflogs = FakeFFLogs(LogsIdentity(900, "111", "Ann Tank", "Exodus"))
    verified(ctx, clock, 5)
    [c] = ch.list_characters(ctx.db, 5)
    assert c["fflogs_id"] == 900

    ctx.fflogs = FakeFFLogs(LogsIdentity(901, "111", "Ann Renamed", "Gilgamesh"))   # renamed + transferred
    ctx.fflogs.by_id[900] = ctx.fflogs.by_lodestone["111"]
    c2, changed = run_async(ch.refresh(ctx.db, ctx.fflogs, c, ctx.now()))
    assert changed and (c2["name"], c2["world"], c2["fflogs_id"]) == ("Ann Renamed", "Gilgamesh", 901)
    assert ctx.fflogs.calls[0] == (None, 900)                       # uses the stored id first


def test_fflogs_trouble_or_unknown_character_leaves_the_row_alone(ctx, clock):
    verified(ctx, clock, 5)
    [c] = ch.list_characters(ctx.db, 5)
    for fl in (None, FakeFFLogs(), FakeFFLogs(error=FFLogsError("down"))):
        same, changed = run_async(ch.refresh(ctx.db, fl, c, ctx.now()))
        assert not changed and same["name"] == "Ann Tank"


def test_a_fflogs_record_for_a_different_lodestone_character_is_ignored(ctx, clock):
    verified(ctx, clock, 5)
    [c] = ch.list_characters(ctx.db, 5)
    other = FakeFFLogs(LogsIdentity(900, "999", "Someone Else", "Exodus"))
    same, changed = run_async(ch.refresh(ctx.db, other, c, ctx.now()))
    assert not changed and same["name"] == "Ann Tank"


def test_parse_identity_follows_canonical_ids():
    data = {"characterData": {"character": {"id": 5, "canonicalID": 9, "name": "A B", "lodestoneID": 1,
                                            "server": {"name": "Exodus"}}}}
    ident, newer = parse_identity(data)
    assert (ident.fflogs_id, ident.lodestone_id, newer) == (9, "1", 9)
    assert parse_identity({"characterData": {"character": None}}) == (None, None)


# ---------------------------------------------------------------- migration from the soft-link version

def test_migration_keeps_verified_rows_and_drops_soft_links():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE players (discord_id TEXT PRIMARY KEY, display_name TEXT);
        CREATE TABLE characters (discord_id TEXT PRIMARY KEY, name TEXT NOT NULL, world TEXT NOT NULL,
            lodestone_id TEXT, verified_utc TEXT, token TEXT, token_utc TEXT);
        CREATE UNIQUE INDEX idx_characters_lodestone ON characters(lodestone_id) WHERE lodestone_id IS NOT NULL;
        CREATE INDEX idx_characters_name ON characters(lower(name), lower(world));
        INSERT INTO characters VALUES ('1', 'Ann Tank', 'Exodus', '111', '2026-10-30T00:00:00+00:00', NULL, NULL);
        INSERT INTO characters VALUES ('2', 'Bob Soft', 'Exodus', NULL, NULL, 'raidbot-x', NULL);""")
    ch.ensure_schema(conn)
    ch.ensure_schema(conn)                                          # idempotent
    rows = conn.execute("SELECT discord_id, name, lodestone_id FROM characters").fetchall()
    assert [tuple(r) for r in rows] == [("1", "Ann Tank", "111")]


# ---------------------------------------------------------------- Lodestone page parsing
# Written from my understanding of Lodestone's markup; replace with saved real pages to be sure.

SEARCH_HTML = """
<ul>
<li class="entry"><a href="/lodestone/character/111/" class="entry__link"><div class="entry__chara__face"><img src="x"></div>
<div class="entry__box entry__box--world"><p class="entry__name">Ann Tank</p>
<p class="entry__world"><i class="x"></i>Exodus&nbsp;[Primal]</p></div></a></li>
<li class="entry"><a href="/lodestone/character/222/" class="entry__link">
<div class="entry__box entry__box--world"><p class="entry__name">Ann Tanker</p>
<p class="entry__world">Exodus [Primal]</p></div></a></li>
<li class="entry"><a href="/lodestone/character/333/" class="entry__link">
<div class="entry__box entry__box--world"><p class="entry__name">Ann Tank</p>
<p class="entry__world">Gilgamesh [Aether]</p></div></a></li>
</ul>"""

PROFILE_HTML = """
<p class="frame__chara__name">Ann Tank</p>
<p class="frame__chara__world"><i class="x"></i>Exodus [Primal]</p>
<div class="character__selfintroduction">Hello<br>raidbot-a1b2c3 &amp; more</div>"""


def test_search_needs_an_exact_name_and_world():
    assert parse_search(SEARCH_HTML, "Ann Tank", "Exodus") == "111"
    assert parse_search(SEARCH_HTML, "ann tank", "exodus") == "111"
    assert parse_search(SEARCH_HTML, "Ann Tank", "Gilgamesh") == "333"
    assert parse_search(SEARCH_HTML, "Ann", "Exodus") is None
    assert parse_search(SEARCH_HTML, "Ann Tank", "Zodiark") is None
    assert parse_search("<html></html>", "Ann Tank", "Exodus") is None


def test_profile_parsing():
    p = parse_profile(PROFILE_HTML, "111")
    assert (p.lodestone_id, p.name, p.world) == ("111", "Ann Tank", "Exodus")
    assert "raidbot-a1b2c3 & more" in p.intro
    assert parse_profile(PROFILE_HTML.replace("character__selfintroduction", "x"), "111").intro == ""
    with pytest.raises(LodestoneError):
        parse_profile("<html>private</html>", "111")
