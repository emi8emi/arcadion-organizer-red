"""The /availability menu: roles, prog checkpoint, day, slots, limits. Every click saves at once."""
from datetime import date

import discord

from .config import ROLES
from .db import parse_iso, split_roles
from .services import checkpoint_label, profile_text, signup_open, tick_lines, validate_limits


class AvailabilityView(discord.ui.View):
    def __init__(self, ctx, run, user_id, display_name):
        super().__init__(timeout=900)
        self.ctx, self.run_id = ctx, run["id"]
        self.user_id, self.display_name = str(user_id), display_name
        self.checkpoints = ctx.db.checkpoints(run["fight_id"])
        self.slots = ctx.db.run_slots(run["id"])
        self.days = sorted({s["slot_key"][:10] for s in self.slots})
        self.day = self.days[0]

        self.role_select = RoleSelect()
        self.prog_select = ProgSelect()
        self.day_select = DaySelect()
        self.slot_select = SlotSelect()
        for item in (self.role_select, self.prog_select, self.day_select, self.slot_select,
                     LimitsButton(), DoneButton()):
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

        self.role_select.options = [discord.SelectOption(label=r.capitalize(), value=r, default=r in roles)
                                    for r in ROLES]
        self.prog_select.options = [
            discord.SelectOption(label=checkpoint_label(self.checkpoints, c["idx"])[:100], value=str(c["idx"]),
                                 default=(c["idx"] == prog)) for c in self.checkpoints[:25]]
        per_day = {d: sum(k[:10] == d for k in ticked) for d in self.days}
        self.day_select.options = [
            discord.SelectOption(label=date.fromisoformat(d).strftime("%a %d %b"), value=d,
                                 description=f"{per_day[d]} ticked", default=(d == self.day))
            for d in self.days[:25]]
        tz_name = self.ctx.cfg["timezone"]
        day_slots = [s for s in self.slots if s["slot_key"][:10] == self.day][:25]
        self.slot_select.options = [
            discord.SelectOption(
                label=parse_iso(s["start_utc"]).astimezone(self._zone()).strftime("%H:%M %Z"),
                value=s["slot_key"], default=(s["slot_key"] in ticked)) for s in day_slots]
        self.slot_select.max_values = max(1, len(day_slots))
        self.slot_select.placeholder = f"Slots you can play on {date.fromisoformat(self.day).strftime('%a %d %b')} ({tz_name})"

    def _zone(self):
        from zoneinfo import ZoneInfo
        return ZoneInfo(self.ctx.cfg["timezone"])

    def render(self):
        run = self.run
        deadline = parse_iso(run["signup_deadline_utc"])
        keys = self.ctx.db.ticked_keys(self.run_id, self.user_id)
        parts = [f"**{run['name']}**: signups close {discord.utils.format_dt(deadline, 'R')}",
                 profile_text(self.ctx, run, self.user_id),
                 "Pick roles and your prog, then a day and the slots you can play. It saves as you click.",
                 f"Slots ticked ({len(keys)}), in your local time:"]
        parts += tick_lines(self.ctx, run, keys) or ["• none yet"]
        return "\n".join(parts)[:1990]

    async def refresh(self, interaction, extra=""):
        self.sync()
        await interaction.response.edit_message(content=(self.render() + extra)[:1990], view=self)

    async def guard(self, interaction):
        """Stop edits once signups close (the menu can stay open for a while)."""
        ok, why = signup_open(self.ctx, self.run)
        if not ok:
            await interaction.response.edit_message(content=why, view=None)
            self.stop()
        return ok

    async def interaction_check(self, interaction):
        return str(interaction.user.id) == self.user_id


class RoleSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Roles you can play", min_values=1, max_values=len(ROLES),
                         options=[discord.SelectOption(label=r, value=r) for r in ROLES], row=0)

    async def callback(self, interaction):
        v = self.view
        if await v.guard(interaction):
            v.ctx.db.upsert_player(v.user_id, v.display_name, roles=self.values, now=v.ctx.now())
            await v.refresh(interaction)


class ProgSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Your prog checkpoint", min_values=1, max_values=1,
                         options=[discord.SelectOption(label="loading", value="0")], row=1)

    async def callback(self, interaction):
        v = self.view
        if await v.guard(interaction):
            v.ctx.db.upsert_player(v.user_id, v.display_name, prog_index=int(self.values[0]), now=v.ctx.now())
            await v.refresh(interaction)


class DaySelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Day to edit", min_values=1, max_values=1,
                         options=[discord.SelectOption(label="loading", value="0")], row=2)

    async def callback(self, interaction):
        v = self.view
        v.day = self.values[0]
        await v.refresh(interaction)


class SlotSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Slots", min_values=0, max_values=1,
                         options=[discord.SelectOption(label="loading", value="0")], row=3)

    async def callback(self, interaction):
        v = self.view
        if await v.guard(interaction):
            v.ctx.db.upsert_player(v.user_id, v.display_name, now=v.ctx.now())
            v.ctx.db.set_day_availability(v.run_id, v.user_id, v.day, self.values)
            await v.refresh(interaction)


class LimitsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Limits and playing lower", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction):
        v = self.view
        if await v.guard(interaction):
            await interaction.response.send_modal(LimitsModal(v))


class DoneButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Done", style=discord.ButtonStyle.success, row=4)

    async def callback(self, interaction):
        v = self.view
        v.stop()
        await interaction.response.edit_message(content=v.render() + "\n\n✅ Saved. Run /availability again to change it.",
                                                view=None)


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
            await v.refresh(interaction)
