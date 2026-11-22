"""The /availability menu: roles, prog checkpoint, day, slots, limits. Every click saves at once."""
import discord

from . import characters as ch
from .config import ROLES, role_label
from .db import parse_iso, split_roles
from .votes import FEEDBACK_CATEGORIES, MAX_NOTE, cast_party_rating, check_rater, submit_feedback, time_reason
from .services import (checkpoint_label, clip_message, day_accepts_signups, day_label, day_state, cutoff_of,
                       profile_text, signup_open, snapshot_note, tick_lines, validate_limits)


NO_ROLES_WARNING = ("⚠ **No roles selected yet.** Press **Roles** and pick every role you can play. "
                    "Until you do, you will not be placed in any party.")


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

        # Dropdowns occupy rows 0 to 3
        self.prog_select = ProgSelect()          # row=0
        self.day_select = DaySelect()            # row=1
        self.character_select = CharacterSelect() # row=2
        self.slot_select = SlotSelect()          # row=3
        
        # Action buttons grouped together on row 4 (the bottom)
        self.roles_button = RolesButton()        # row=4
        self.limits_button = LimitsButton()      # row=4
        self.done_button = DoneButton()          # row=4

        for item in (
            self.prog_select, 
            self.day_select, 
            self.character_select, 
            self.slot_select,
            self.roles_button, 
            self.limits_button, 
            self.done_button
        ):
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

        self.roles_button.label = f"Roles ({len(roles)})" if roles else "Roles ⚠ none"
        self.roles_button.style = discord.ButtonStyle.success if roles else discord.ButtonStyle.danger
        
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
        self.slot_select.placeholder = (f"Slots: {day_label(self.day)} ({tz_name})" if day_open
                                        else f"{day_label(self.day)} (locked)")
        self._sync_character(day_open)

    def _sync_character(self, day_open):
        """The character dropdown shows the player's verified characters, with the one used on the edited day selected."""
        mine = ch.list_characters(self.ctx.db, self.user_id)
        if not mine:
            self.character_select.options = [discord.SelectOption(
                label="No verified character", value="-", description="Use /character add")]
            self.character_select.disabled = True
            self.character_select.placeholder = "Character (none)"
            return
        picked = self.ctx.db.chosen_character(self.run_id, self.day, self.user_id)
        self.character_select.options = [
            discord.SelectOption(label=ch.label(c)[:100], value=str(c["id"]),
                                 default=bool(picked) and picked["id"] == c["id"]) for c in mine]
        self.character_select.disabled = not day_open               # a closed day keeps its frozen character
        self.character_select.placeholder = (f"Character for {day_label(self.day)}" if day_open
                                             else f"{day_label(self.day)} character (locked)")

    def _character_line(self):
        char = self.ctx.db.chosen_character(self.run_id, self.day, self.user_id)
        if char:
            return f"Char ({day_label(self.day)}): **{ch.label(char)}**"
        if not ch.list_characters(self.ctx.db, self.user_id):
            return "Char: None verified (/character add)"
        return f"Char: Not set for {day_label(self.day)}"

    def _day_description(self, run, day_row, n_ticked):
        """Plain text for the day dropdown (Discord timestamps do not render inside dropdowns)."""
        state = day_state(self.ctx, run, day_row)
        if state == "open":
            local = cutoff_of(day_row).astimezone(self._zone()).strftime("%a %H:%M %Z")
            return f"{n_ticked} ticked · closes {local}"[:100]
        return f"{n_ticked} ticked · {state}"

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
                day_lines.append(f"{day_label(r['day'])}: {discord.utils.format_dt(cutoff_of(r), 'R')}")
            else:
                day_lines.append(f"{day_label(r['day'])}: closed")
        parts = [] if split_roles(player["roles"] if player else "") else [NO_ROLES_WARNING]
        parts += [
            f"**{run['name']}** | Editing **{day_label(self.day)}** (closes {discord.utils.format_dt(cutoff_of(picked), 'R')})",
            f"Prog: **{checkpoint_label(self.checkpoints, prog)}** | {self._character_line()}",
            f"Ticked: {len(keys)} slots | Cutoffs: " + " | ".join(day_lines)
        ]
        parts += tick_lines(self.ctx, run, keys, limit=18) or ["• none"]
        return clip_message("\n".join(parts), limit)

    async def refresh(self, interaction, extra=""):
        self.sync()
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
        """Like guard, but also refuses the day being edited when that day alone has closed."""
        if not await self.guard(interaction):
            return False
        ok, why = signup_open(self.ctx, self.run, self.day)
        if not ok:
            await self.refresh(interaction, extra=f"\n⚠ {why}")
        return ok

    async def interaction_check(self, interaction):
        return str(interaction.user.id) == self.user_id


class ProgSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Prog checkpoint", min_values=1, max_values=1,
                         options=[discord.SelectOption(label="loading", value="0")], row=0)

    async def callback(self, interaction):
        v = self.view
        if await v.guard(interaction):
            v.ctx.db.upsert_player(v.user_id, v.display_name, prog_index=int(self.values[0]), now=v.ctx.now())
            await v.refresh_after_edit(interaction)


class DaySelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Day to edit", min_values=1, max_values=1,
                         options=[discord.SelectOption(label="loading", value="0")], row=1)

    async def callback(self, interaction):
        v = self.view
        v.day = self.values[0]
        await v.refresh(interaction)


class CharacterSelect(discord.ui.Select):
    """Which verified character the player brings on the day being edited (saved per day)."""

    def __init__(self):
        super().__init__(placeholder="Character", min_values=1, max_values=1,
                         options=[discord.SelectOption(label="loading", value="0")], row=2)

    async def callback(self, interaction):
        v = self.view
        if await v.guard_day(interaction):
            char = ch.get_character(v.ctx.db, v.user_id, int(self.values[0]))
            if char is None:
                return await v.refresh(interaction, extra="\n⚠ Invalid character.")
            v.ctx.db.upsert_player(v.user_id, v.display_name, now=v.ctx.now())
            v.ctx.db.set_signup_character(v.run_id, [v.day], v.user_id, char["id"])
            await v.refresh(interaction)


class SlotSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Slots", min_values=0, max_values=1,
                         options=[discord.SelectOption(label="loading", value="0")], row=3)

    async def callback(self, interaction):
        v = self.view
        if await v.guard_day(interaction):
            v.ctx.db.upsert_player(v.user_id, v.display_name, now=v.ctx.now())
            v.ctx.db.set_day_availability(v.run_id, v.user_id, v.day, self.values)
            await v.refresh(interaction)


class RolesButton(discord.ui.Button):
    """Opens the roles modal."""
    def __init__(self):
        super().__init__(label="Roles", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction):
        v = self.view
        if await v.guard(interaction):
            await interaction.response.send_modal(RolesModal(v))


class LimitsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Limits", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction):
        v = self.view
        if await v.guard(interaction):
            player = v.ctx.db.get_player(v.user_id)
            if not player or player["prog_index"] is None:
                return await v.refresh(interaction, extra="\n⚠ Pick prog first.")
            await interaction.response.send_modal(LimitsModal(v))


class DoneButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Done", style=discord.ButtonStyle.primary, row=4)

    async def callback(self, interaction):
        v = self.view
        v.stop()
        player = v.ctx.db.get_player(v.user_id)
        done = "\n\nSaved. Run /availability to edit."
        if not split_roles(player["roles"] if player else ""):
            done = "\n\n⚠ Saved, but you still have **no roles selected**, so you will not be placed in a party. " \
                   "Run /availability and press Roles."
        await interaction.response.edit_message(content=v.render(1990 - len(done)) + done, view=None)


class RolesModal(discord.ui.Modal):
    """Every role the player can play (flex players pick several). At least one stays selected."""

    def __init__(self, view):
        super().__init__(title="Roles you can play")
        self.owner = view
        player = view.ctx.db.get_player(view.user_id)
        have = set(split_roles(player["roles"]) if player else ROLES)
        self.select = discord.ui.Select(
            min_values=1, max_values=len(ROLES),
            options=[discord.SelectOption(label=role_label(r), value=r, default=r in have) for r in ROLES])
        self.add_item(discord.ui.Label(
            text="Roles", component=self.select,
            description="Please pick all the roles you're willing to play."))

    async def on_submit(self, interaction):
        v = self.owner
        roles = {r for r in self.select.values if r in ROLES}
        if not roles:
            await interaction.response.send_message("Keep at least one role.", ephemeral=True)
            return
        if await v.guard(interaction):
            v.ctx.db.upsert_player(v.user_id, v.display_name, roles=sorted(roles), now=v.ctx.now())
            await v.refresh_after_edit(interaction)

class LimitsModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Limits and playing lower")
        self.owner = view
        db = view.ctx.db
        lim = db.get_limits(view.run_id, view.user_id, view.ctx.cfg)
        prog = db.get_player(view.user_id)["prog_index"]

        self.per_day = discord.ui.TextInput(
            label="Max parties per day",
            default=str(lim["max_per_day"]),
            max_length=2
        )

        self.per_run = None
        if view.ctx.cfg.get("enable_per_run_limit", False):
            self.per_run = discord.ui.TextInput(
                label="Max parties per run",
                default=str(lim["max_per_run"]),
                max_length=2
            )

        reachable = [c for c in view.checkpoints if c["idx"] <= prog][-25:]
        current_min = max(prog - lim["drop_checkpoints"], reachable[0]["idx"])

        self.min_select = discord.ui.Select(
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=(
                        checkpoint_label(view.checkpoints, c["idx"])
                        + (" (my prog: no lower)" if c["idx"] == prog else "")
                    )[:100],
                    value=str(c["idx"]),
                    default=c["idx"] == current_min
                )
                for c in reachable
            ]
        )

        self.add_item(self.per_day)

        if self.per_run is not None:
            self.add_item(self.per_run)

        self.add_item(
            discord.ui.Label(
                text="Lowest checkpoint you accept",
                component=self.min_select,
                description="Pick your own prog if you never want to play lower."
            )
        )

    async def on_submit(self, interaction):
        v = self.owner

        try:
            per_day = int(self.per_day.value)
            per_run = int(self.per_run.value) if self.per_run is not None else 99
        except ValueError:
            await interaction.response.send_message(
                "Those need to be whole numbers.",
                ephemeral=True
            )
            return

        player = v.ctx.db.get_player(v.user_id)
        prog = player["prog_index"] if player else None

        if prog is None:
            await interaction.response.send_message(
                "Pick your prog checkpoint first, then set your limits.",
                ephemeral=True
            )
            return

        drop = max(0, prog - int(self.min_select.values[0]))

        err = validate_limits(per_day, per_run, drop, len(v.checkpoints))
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        if await v.guard(interaction):
            v.ctx.db.upsert_player(
                v.user_id,
                v.display_name,
                now=v.ctx.now()
            )
            v.ctx.db.set_limits(
                v.run_id,
                v.user_id,
                v.ctx.cfg,
                max_per_day=per_day,
                max_per_run=per_run,
                drop_checkpoints=drop
            )
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


RATING_BUTTONS = {"like": ("Liked it", "👍", discord.ButtonStyle.success),
                  "dislike": ("Didn't enjoy it", "👎", discord.ButtonStyle.danger)}


class RateView(discord.ui.View):
    """Ephemeral: rate the party as a whole (👍 / 👎), or give feedback about a problem or a player."""

    def __init__(self, ctx, party_id, user_id):
        super().__init__(timeout=900)
        self.ctx, self.party_id, self.user_id = ctx, party_id, str(user_id)
        self.notice = ""
        self.title = party_title(ctx.db.party(party_id))
        self.rating = ctx.db.party_rating(party_id, self.user_id)
        self.buttons = {r: PartyRatingButton(r) for r in RATING_BUTTONS}
        for b in self.buttons.values():
            self.add_item(b)
        self.add_item(FeedbackButton())
        self.sync()

    def sync(self):
        for rating, b in self.buttons.items():
            b.style = RATING_BUTTONS[rating][2] if rating == self.rating else discord.ButtonStyle.secondary

    def render(self):
        lines = [f"**How was your party overall?** {self.title}",
                 "Tap 👍 or 👎 for the group as a whole. Use **Give feedback** to point out a problem or a player. "
                 "Only organizers ever see feedback, and never your name."]
        if self.rating:
            lines.append(f"Your rating: {RATING_BUTTONS[self.rating][1]} (tap the other one to change it)")
        if self.notice:
            lines.append(self.notice)
        return "\n".join(lines)[:1990]

    async def interaction_check(self, interaction):
        return str(interaction.user.id) == self.user_id

    async def redraw(self, interaction):
        self.sync()
        await interaction.response.edit_message(content=self.render(), view=self)


class PartyRatingButton(discord.ui.Button):
    def __init__(self, rating):
        label, emoji, _ = RATING_BUTTONS[rating]
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary)
        self.rating = rating

    async def callback(self, interaction):
        v = self.view
        ok, why = cast_party_rating(v.ctx, v.user_id, v.party_id, self.rating)
        if ok:
            v.rating, v.notice = self.rating, ""
        else:
            v.notice = f"⚠ {why} Nothing was saved."
        await v.redraw(interaction)


class FeedbackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Give feedback", style=discord.ButtonStyle.primary)

    async def callback(self, interaction):
        v = self.view
        _, why = check_rater(v.ctx.db, v.ctx.cfg, v.ctx.now(), v.user_id, v.party_id)
        if why:
            v.notice = f"⚠ {why}"
            return await v.redraw(interaction)
        await interaction.response.send_modal(FeedbackModal(v.ctx, v.party_id, v.user_id))


class FeedbackModal(discord.ui.Modal):
    """Who it is about (optional), what happened, and details. Sent to organizers without the reporter's name."""

    def __init__(self, ctx, party_id, user_id):
        super().__init__(title="Feedback about your party")
        self.ctx, self.party_id, self.user_id = ctx, party_id, str(user_id)
        others = [m for m in ctx.db.party_members(party_id) if m["discord_id"] != self.user_id][:25]
        every = ctx.db.party_feedback_by(party_id, user_id)
        last = max(every, key=lambda r: r["id"])["category"] if every else None   # start from the latest one sent;
        earlier = [r for r in every if r["category"] == last]                      # the same category replaces it
        named = {r["target_id"] for r in earlier}
        self.players = discord.ui.Select(
            min_values=0, max_values=max(1, len(others)), required=False,
            options=[discord.SelectOption(
                label=((ctx.db.get_player(m["discord_id"]) or {"display_name": "Player"})["display_name"]
                       + " · " + role_label(m["role"]))[:100],
                value=m["discord_id"], default=m["discord_id"] in named) for m in others]
            or [discord.SelectOption(label="Nobody else", value="none")])
        self.category = discord.ui.Select(
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=label, value=key, default=bool(earlier) and earlier[0]["category"] == key)
                     for key, (label, _) in FEEDBACK_CATEGORIES.items()])
        self.note = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=MAX_NOTE,
                                         default=(earlier[0]["note"] if earlier else None) or None,
                                         placeholder="What happened? Organizers read this.")
        self.add_item(discord.ui.Label(text="Who is it about?", component=self.players,
                                       description="Leave empty if it is about the party in general."))
        self.add_item(discord.ui.Label(text="What happened?", component=self.category))
        self.add_item(discord.ui.Label(text="Details (optional)", component=self.note))

    async def on_submit(self, interaction):
        targets = [v for v in self.players.values if v != "none"]
        ok, why = submit_feedback(self.ctx, self.user_id, self.party_id, targets, self.category.values[0],
                                  self.note.value)
        await interaction.response.send_message(
            "Thanks, the organizers will see this (without your name). Sending the same category again replaces it; "
            "another category is added next to it."
            if ok else f"⚠ {why} Nothing was saved.", ephemeral=True)


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