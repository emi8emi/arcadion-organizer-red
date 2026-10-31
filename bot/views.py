"""The /availability menu: roles, prog checkpoint, day, slots, limits. Every click saves at once."""
import discord

from . import characters as ch
from .config import ROLES
from .db import parse_iso, split_roles
from .votes import cast_vote, current_rating, time_reason
from .services import (checkpoint_label, clip_message, day_accepts_signups, day_label, day_state, cutoff_of,
                       profile_text, signup_open, snapshot_note, tick_lines, validate_limits)


class AvailabilityView(discord.ui.View):
    def __init__(self, ctx, run, user_id, display_name):
        super().__init__(timeout=900)
        self.ctx, self.run_id = ctx, run["id"]
        self.user_id, self.display_name = str(user_id), display_name
        self.checkpoints = ctx.db.checkpoints(run["fight_id"])
        self.slots = ctx.db.run_slots(run["id"])
        self.days = sorted({s["slot_key"][:10] for s in self.slots})
        self.day = next((d["day"] for d in ctx.db.run_days(run["id"])
                         if day_accepts_signups(ctx, run, d)), self.days[0])     # first day still open

        # Discord allows 5 rows, and a select takes a whole row, so roles are toggle buttons that share row 0
        # with Limits and Done. Rows 1-4: prog, day, the character for that day, the day's slots.
        self.role_buttons = [RoleButton(r) for r in ROLES]
        self.prog_select = ProgSelect()
        self.day_select = DaySelect()
        self.character_select = CharacterSelect()
        self.slot_select = SlotSelect()
        for item in (*self.role_buttons, LimitsButton(), DoneButton(), self.prog_select, self.day_select,
                     self.character_select, self.slot_select):
            self.add_item(item)
        self.sync()

    # --- state -> components

    @property
    def run(self):
        return self.ctx.db.run(self.run_id)

    def sync(self):
        """Rebuild every select's options so the defaults match what is saved."""
        db = self.ctx.db
        player = db.get_player(self.user_id)
        roles = split_roles(player["roles"]) if player else []
        prog = player["prog_index"] if player else None
        ticked = set(db.ticked_keys(self.run_id, self.user_id))

        for b in self.role_buttons:
            b.style = discord.ButtonStyle.success if b.role in roles else discord.ButtonStyle.secondary
        self.prog_select.options = [
            discord.SelectOption(label=checkpoint_label(self.checkpoints, c["idx"])[:100], value=str(c["idx"]),
                                 default=(c["idx"] == prog)) for c in self.checkpoints[:25]]
        per_day = {d: sum(k[:10] == d for k in ticked) for d in self.days}
        run = self.run
        rows = {r["day"]: r for r in db.run_days(self.run_id)}
        self.day_select.options = [
            discord.SelectOption(label=day_label(d), value=d, description=self._day_description(run, rows[d], per_day[d]),
                                 default=(d == self.day))
            for d in self.days[:25]]
        tz_name = self.ctx.cfg["timezone"]
        day_slots = [s for s in self.slots if s["slot_key"][:10] == self.day][:25]
        self.slot_select.options = [
            discord.SelectOption(
                label=parse_iso(s["start_utc"]).astimezone(self._zone()).strftime("%H:%M %Z"),
                value=s["slot_key"], default=(s["slot_key"] in ticked)) for s in day_slots]
        self.slot_select.max_values = max(1, len(day_slots))
        day_open = day_accepts_signups(self.ctx, run, rows[self.day])
        self.slot_select.disabled = not day_open                  # a closed day keeps its ticks, read-only
        self.slot_select.placeholder = (f"Slots you can play on {day_label(self.day)} ({tz_name})" if day_open
                                        else f"{day_label(self.day)} is closed: its slots are locked")
        self._sync_character(day_open)

    def _sync_character(self, day_open):
        """The character dropdown shows the player's verified characters, with the one used on the edited day selected."""
        mine = ch.list_characters(self.ctx.db, self.user_id)
        if not mine:
            self.character_select.options = [discord.SelectOption(
                label="No verified character yet", value="-", description="Use /character add, then /character verify")]
            self.character_select.disabled = True
            self.character_select.placeholder = "Character (none verified yet)"
            return
        picked = self.ctx.db.chosen_character(self.run_id, self.day, self.user_id)
        self.character_select.options = [
            discord.SelectOption(label=ch.label(c)[:100], value=str(c["id"]),
                                 default=bool(picked) and picked["id"] == c["id"]) for c in mine]
        self.character_select.disabled = not day_open               # a closed day keeps its frozen character
        self.character_select.placeholder = (f"Which character do you play on {day_label(self.day)}?" if day_open
                                             else f"{day_label(self.day)} is closed: its character is locked")

    def _character_line(self):
        char = self.ctx.db.chosen_character(self.run_id, self.day, self.user_id)
        if char:
            return f"Character on {day_label(self.day)}: **{ch.label(char)}**"
        if not ch.list_characters(self.ctx.db, self.user_id):
            return "⚠ You have no verified character yet: use /character add. Organizers will see you without one."
        return f"⚠ Pick which character you play on {day_label(self.day)} (the Character menu)."

    def _day_description(self, run, day_row, n_ticked):
        """Plain text for the day dropdown (Discord timestamps do not render inside dropdowns)."""
        state = day_state(self.ctx, run, day_row)
        if state == "open":
            local = cutoff_of(day_row).astimezone(self._zone()).strftime("%a %H:%M %Z")
            return f"{n_ticked} ticked · closes {local}"[:100]
        return f"{n_ticked} ticked · {'not open yet' if state == 'draft' else 'closed'}"

    def _zone(self):
        from zoneinfo import ZoneInfo
        return ZoneInfo(self.ctx.cfg["timezone"])

    def render(self, limit=1990):
        run = self.run
        db = self.ctx.db
        rows = db.run_days(self.run_id)
        picked = next(r for r in rows if r["day"] == self.day)
        player = db.get_player(self.user_id)
        prog = player["prog_index"] if player else None
        keys = db.ticked_keys(self.run_id, self.user_id)

        day_lines = []
        for r in rows:
            if day_accepts_signups(self.ctx, run, r):
                day_lines.append(f"{day_label(r['day'])}: closes {discord.utils.format_dt(cutoff_of(r), 'R')}")
            else:
                day_lines.append(f"{day_label(r['day'])}: closed")
        parts = [f"**{run['name']}**: each day closes the evening before. Editing **{day_label(self.day)}**, "
                 f"signups close {discord.utils.format_dt(cutoff_of(picked), 'R')}.",
                 f"Your prog right now: **{checkpoint_label(self.checkpoints, prog)}**. "
                 "It changes often, so check it before each day closes.",
                 profile_text(self.ctx, run, self.user_id),
                 self._character_line(),
                 "Pick roles and your prog, then a day and the slots you can play. It saves as you click.",
                 "Cutoffs:", *day_lines,
                 f"Slots ticked ({len(keys)}), in your local time:"]
        parts += tick_lines(self.ctx, run, keys, limit=18) or ["• none yet"]
        return clip_message("\n".join(parts), limit)

    async def refresh(self, interaction, extra=""):
        self.sync()
        # the extra line is appended after clipping, so a long menu can never push it out of the message
        await interaction.response.edit_message(content=self.render(1990 - len(extra)) + extra, view=self)

    async def refresh_after_edit(self, interaction):
        """After a change to roles, prog or limits: tell the player if some of their days are already frozen."""
        note = snapshot_note(self.ctx, self.run, self.user_id)
        await self.refresh(interaction, extra=f"\nℹ {note}" if note else "")

    async def guard(self, interaction):
        """Stop edits once every day has closed (the menu can stay open for a while)."""
        ok, why = signup_open(self.ctx, self.run)
        if not ok:
            await interaction.response.edit_message(content=why, view=None)
            self.stop()
        return ok

    async def guard_day(self, interaction):
        """Like guard, but also refuses the day being edited when that day alone has closed.

        The menu stays open so the player can switch to a day that is still open.
        """
        if not await self.guard(interaction):
            return False
        ok, why = signup_open(self.ctx, self.run, self.day)
        if not ok:
            await self.refresh(interaction, extra=f"\n⚠ {why} Nothing was changed.")
        return ok

    async def interaction_check(self, interaction):
        return str(interaction.user.id) == self.user_id


class RoleButton(discord.ui.Button):
    """One toggle per role: green = you can play it. At least one role stays on."""

    def __init__(self, role):
        super().__init__(label="DPS" if role == "dps" else role.capitalize(), style=discord.ButtonStyle.secondary,
                         row=0)
        self.role = role

    async def callback(self, interaction):
        v = self.view
        if not await v.guard(interaction):
            return
        player = v.ctx.db.get_player(v.user_id)
        roles = set(split_roles(player["roles"]) if player else [])
        roles = roles - {self.role} if self.role in roles else roles | {self.role}
        if not roles:
            return await v.refresh(interaction, extra="\n⚠ Keep at least one role on.")
        v.ctx.db.upsert_player(v.user_id, v.display_name, roles=sorted(roles), now=v.ctx.now())
        await v.refresh_after_edit(interaction)


class ProgSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Your prog checkpoint", min_values=1, max_values=1,
                         options=[discord.SelectOption(label="loading", value="0")], row=1)

    async def callback(self, interaction):
        v = self.view
        if await v.guard(interaction):
            v.ctx.db.upsert_player(v.user_id, v.display_name, prog_index=int(self.values[0]), now=v.ctx.now())
            await v.refresh_after_edit(interaction)


class DaySelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Day to edit", min_values=1, max_values=1,
                         options=[discord.SelectOption(label="loading", value="0")], row=2)

    async def callback(self, interaction):
        v = self.view
        v.day = self.values[0]
        await v.refresh(interaction)


class CharacterSelect(discord.ui.Select):
    """Which verified character the player brings on the day being edited (saved per day)."""

    def __init__(self):
        super().__init__(placeholder="Character", min_values=1, max_values=1,
                         options=[discord.SelectOption(label="loading", value="0")], row=3)

    async def callback(self, interaction):
        v = self.view
        if await v.guard_day(interaction):
            char = ch.get_character(v.ctx.db, v.user_id, int(self.values[0]))     # must be one of their own
            if char is None:
                return await v.refresh(interaction, extra="\n⚠ That is not one of your verified characters.")
            v.ctx.db.upsert_player(v.user_id, v.display_name, now=v.ctx.now())
            v.ctx.db.set_signup_character(v.run_id, [v.day], v.user_id, char["id"])
            await v.refresh(interaction)


class SlotSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Slots", min_values=0, max_values=1,
                         options=[discord.SelectOption(label="loading", value="0")], row=4)

    async def callback(self, interaction):
        v = self.view
        if await v.guard_day(interaction):
            v.ctx.db.upsert_player(v.user_id, v.display_name, now=v.ctx.now())
            v.ctx.db.set_day_availability(v.run_id, v.user_id, v.day, self.values)
            await v.refresh(interaction)


class LimitsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Limits", style=discord.ButtonStyle.secondary, row=0)

    async def callback(self, interaction):
        v = self.view
        if await v.guard(interaction):
            await interaction.response.send_modal(LimitsModal(v))


class DoneButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Done", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction):
        v = self.view
        v.stop()
        done = "\n\n✅ Saved. Run /availability again to change it."
        await interaction.response.edit_message(content=v.render(1990 - len(done)) + done, view=None)


class LimitsModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Limits and playing lower")
        self.owner = view
        lim = view.ctx.db.get_limits(view.run_id, view.user_id, view.ctx.cfg)
        self.per_day = discord.ui.TextInput(label="Max parties per day", default=str(lim["max_per_day"]),
                                            max_length=2)
        self.per_run = discord.ui.TextInput(label="Max parties for the whole run", default=str(lim["max_per_run"]),
                                            max_length=2)
        self.drop = discord.ui.TextInput(
            label="OK with playing lower progs (0 = no)", default=str(lim["drop_checkpoints"]),
            max_length=2)
        for item in (self.per_day, self.per_run, self.drop):
            self.add_item(item)

    async def on_submit(self, interaction):
        v = self.owner
        try:
            per_day, per_run, drop = (int(x.value) for x in (self.per_day, self.per_run, self.drop))
        except ValueError:
            await interaction.response.send_message("Those need to be whole numbers.", ephemeral=True)
            return
        err = validate_limits(per_day, per_run, drop, len(v.checkpoints))
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        if await v.guard(interaction):
            v.ctx.db.upsert_player(v.user_id, v.display_name, now=v.ctx.now())
            v.ctx.db.set_limits(v.run_id, v.user_id, v.ctx.cfg, max_per_day=per_day, max_per_run=per_run,
                                drop_checkpoints=drop)
            await v.refresh_after_edit(interaction)


# ---------------------------------------------------------------- rating (private, ephemeral only)

def party_title(party):
    """'Mon 02 Nov 17:00 · party 1' (local slot time, short enough for a button label)."""
    return f"{_party_day(party)} {party['slot_key'][11:16]} · party {party['party_index'] + 1}"


def _party_day(party):
    from .services import day_label
    return day_label(party["slot_key"][:10])


class RatePartyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rate:(?P<party>[0-9]+)"):
    """The "Rate party N" button in a slot's thread. It survives bot restarts (the id carries the party)."""

    def __init__(self, party_id, label="Rate your party"):
        super().__init__(discord.ui.Button(label=label[:80], style=discord.ButtonStyle.primary,
                                           custom_id=f"rate:{party_id}"))
        self.party_id = int(party_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["party"]))

    async def callback(self, interaction):
        await open_rating(interaction.client.ctx, interaction, self.party_id)


class RatePromptView(discord.ui.View):
    """One "Rate party N" button per party of the slot (Discord allows 25 buttons in a message)."""

    def __init__(self, parties):
        super().__init__(timeout=None)
        for p in list(parties)[:25]:
            self.add_item(RatePartyButton(p["id"], f"Rate party {p['party_index'] + 1}"))


async def open_rating(ctx, interaction, party_id):
    """Open the private rating view for one party, if this player was in it and ratings are open."""
    party = ctx.db.party(party_id)
    me = str(interaction.user.id)
    members = ctx.db.party_members(party_id) if party else []
    if party is None or party["status"] not in ("confirmed", "done") or me not in {m["discord_id"] for m in members}:
        await interaction.response.send_message("You were not in that party.", ephemeral=True)
        return
    why = time_reason(party, ctx.cfg, ctx.now())
    if why:
        await interaction.response.send_message(why, ephemeral=True)
        return
    view = RateView(ctx, party_id, me)
    await interaction.response.send_message(view.render(), view=view, ephemeral=True)


RATING_EMOJI = {"like": "👍", "neutral": "😐", "dislike": "👎"}
RATING_STYLE = {"like": discord.ButtonStyle.success, "neutral": discord.ButtonStyle.secondary,
                "dislike": discord.ButtonStyle.danger}
NEXT_RATING = {"neutral": "like", "like": "dislike", "dislike": "neutral"}   # one tap = next rating


class RateView(discord.ui.View):
    """Ephemeral: one button per other member. Tap a name to cycle their rating 😐 -> 👍 -> 👎 -> 😐
    (each tap is saved at once). Ratings are read from the database once, when the view opens."""

    def __init__(self, ctx, party_id, user_id):
        super().__init__(timeout=900)
        self.ctx, self.party_id, self.user_id = ctx, party_id, str(user_id)
        self.notice = ""
        self.title = party_title(ctx.db.party(party_id))
        self.members = [m for m in ctx.db.party_members(party_id) if m["discord_id"] != self.user_id][:20]
        now = ctx.now()
        self.ratings = {m["discord_id"]: current_rating(ctx.db, self.user_id, m["discord_id"], now)
                        for m in self.members}
        self.buttons = []
        for i, m in enumerate(self.members):
            name = (ctx.db.get_player(m["discord_id"]) or {"display_name": "Player"})["display_name"]
            button = MemberRateButton(m["discord_id"], f"{name} · {m['role']}"[:80], row=i // 5)
            self.buttons.append(button)
            self.add_item(button)
        self.add_item(RateDoneButton())
        self.sync()

    def sync(self):
        for b in self.buttons:
            rating = self.ratings[b.target_id]
            b.emoji, b.style = RATING_EMOJI[rating], RATING_STYLE[rating]

    def render(self):
        lines = [f"**Rate your party:** {self.title}",
                 "Tap a player to change your rating: 😐 neutral → 👍 like → 👎 dislike → 😐. "
                 "Saved at once. Only you can see this, and you can change it until the window closes."]
        if self.notice:
            lines.append(self.notice)
        return "\n".join(lines)[:1990]

    async def interaction_check(self, interaction):
        return str(interaction.user.id) == self.user_id

    async def redraw(self, interaction):
        self.sync()
        await interaction.response.edit_message(content=self.render(), view=self)


class MemberRateButton(discord.ui.Button):
    def __init__(self, target_id, label, row):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, emoji=RATING_EMOJI["neutral"], row=row)
        self.target_id = target_id

    async def callback(self, interaction):
        v = self.view
        new = NEXT_RATING[v.ratings[self.target_id]]
        ok, why = cast_vote(v.ctx, v.user_id, self.target_id, new, v.party_id)
        if ok:
            v.ratings[self.target_id], v.notice = new, ""
        else:
            v.notice = f"⚠ {why} Nothing was saved."
        await v.redraw(interaction)


class RateDoneButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Done", style=discord.ButtonStyle.primary, row=4)

    async def callback(self, interaction):
        self.view.stop()
        await interaction.response.edit_message(content="✅ Saved. Press the button again any time before the window closes.",
                                                view=None)


class RateListView(discord.ui.View):
    """For /rate: one button per party the player can still rate."""

    def __init__(self, ctx, parties):
        super().__init__(timeout=900)
        self.ctx = ctx
        for p in parties[:20]:
            self.add_item(RateListButton(p["party_id"], party_title(p)))


class RateListButton(discord.ui.Button):
    def __init__(self, party_id, label):
        super().__init__(label=label[:80], style=discord.ButtonStyle.primary)
        self.party_id = party_id

    async def callback(self, interaction):
        await open_rating(self.view.ctx, interaction, self.party_id)
