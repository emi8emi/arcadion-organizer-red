"""Shared fixtures: an in-memory bot context, a fixed clock, and fake Discord interactions."""
import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config          # noqa: E402
from bot.db import DB                        # noqa: E402
from bot.grid import GridConfig, run_slots   # noqa: E402
from bot.services import Ctx                 # noqa: E402
from bot.solver_io import build_signups, fingerprint   # noqa: E402

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
ORGANIZER_ROLE_ID = 777


def run_async(coro):
    return asyncio.run(coro)


class FakeResponse:
    """Stands in for discord.InteractionResponse: records what the handler did."""

    def __init__(self):
        self.done = False
        self.sent, self.edits, self.modals = [], [], []

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.sent.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs):
        self.done = True
        self.edits.append(kwargs)

    async def defer(self, **kwargs):
        self.done = True

    async def send_modal(self, modal):
        self.done = True
        self.modals.append(modal)


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class FakeInteraction:
    def __init__(self, user_id=1, name="Tester", organizer=False, role_ids=(), guild_id=100):
        perms = SimpleNamespace(manage_guild=organizer, administrator=False)
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id, display_name=name, guild_permissions=perms,
                                    roles=[SimpleNamespace(id=r) for r in role_ids])
        self.response, self.followup = FakeResponse(), FakeFollowup()

    @property
    def texts(self):
        """Every text the user would see, in order."""
        out = [m["content"] for m in self.response.sent] + [m["content"] for m in self.response.edits if "content" in m]
        return out + [m["content"] for m in self.followup.sent]

    @property
    def text(self):
        return "\n".join(t for t in self.texts if t)


class FakeAttachment:
    def __init__(self, data: bytes, filename="draft.json", size=None):
        self._data, self.filename = data, filename
        self.size = len(data) if size is None else size

    async def read(self):
        return self._data


def organizer(**kw):
    return FakeInteraction(organizer=True, **kw)


def player(user_id, name=None, **kw):
    return FakeInteraction(user_id=user_id, name=name or f"Player {user_id}", **kw)


@pytest.fixture
def cfg(tmp_path):
    c = load_config(env={})
    c["db_path"] = ":memory:"
    c["export_dir"] = str(tmp_path / "exports")
    return c


@pytest.fixture
def clock():
    return {"now": NOW}


@pytest.fixture
def ctx(cfg, clock):
    return Ctx(db=DB(":memory:"), cfg=cfg, grid=GridConfig.from_config(cfg), now=lambda: clock["now"])


@pytest.fixture
def make_run(ctx):
    """Create a run directly in the DB. Days 2026-11-01 (a DST change day) to 2026-11-03 by default."""
    def _make(name="UWU week 1", start=date(2026, 11, 1), end=date(2026, 11, 3), status="open", guild_id=100,
              earliest=None, latest=None, deadline=None):
        slots = run_slots(start, end, ctx.grid, earliest, latest)
        run_id = ctx.db.create_run(
            guild_id=guild_id, name=name, fight_id=1, start_date=start, end_date=end,
            deadline_utc=deadline or datetime(2026, 10, 30, 22, 0, tzinfo=timezone.utc), slots=slots,
            created_by=1, earliest=earliest, latest=latest, now=NOW)
        if status != "draft":
            ctx.db.set_run_status(run_id, status)
        return ctx.db.run(run_id)
    return _make


def add_player(ctx, run, uid, roles, prog, keys, name=None, per_day=2, per_run=4, drop=0):
    """Register a player with a profile, ticks and limits."""
    ctx.db.upsert_player(uid, name or f"Player {uid}", roles=roles, prog_index=prog, now=NOW)
    for day in sorted({k[:10] for k in keys}):
        ctx.db.set_day_availability(run["id"], uid, day, [k for k in keys if k[:10] == day])
    ctx.db.set_limits(run["id"], uid, ctx.cfg, max_per_day=per_day, max_per_run=per_run, drop_checkpoints=drop)


def fill_one_party(ctx, run, slot_key="2026-11-02T17:00", first_id=1, prog=0):
    """Eight players (2 tank, 2 healer, 4 dps) who all ticked the same slot."""
    roles = ["tank"] * 2 + ["healer"] * 2 + ["dps"] * 4
    for i, r in enumerate(roles):
        add_player(ctx, run, first_id + i, [r], prog, [slot_key])
    return list(range(first_id, first_id + 8))


SLOT = "2026-11-02T17:00"


def draft_for(ctx, run, ids, **overrides):
    """A hand-made solver draft for the current signups: one party, ids in role order."""
    data, _ = build_signups(ctx.db, run, ctx.cfg)
    roles = ["tank"] * 2 + ["healer"] * 2 + ["dps"] * 4
    draft = {"version": 1, "input_fingerprint": fingerprint(data),
             "parties": [{"slot": SLOT, "party": 0, "target": {"index": 0, "name": "P1 Fresh"},
                          "members": [{"player": i, "role": r, "helper": False, "placed_down": False, "distance": 0}
                                      for i, r in zip(ids, roles)]}],
             "unfilled_slots": [{"slot": "2026-11-02T18:30", "reason": "Only 0 signed up; 8 needed."}],
             "unmatched_players": []}
    draft.update(overrides)
    return draft
