"""/run edit: one private panel to open, close and adjust an event.

The functions at the top do the work and return the text to show; the panel (`RunEditView`) and the plain handler
entry points (`handlers.run_open`, `run_close`, `run_extend`) both call them, so they cannot disagree.
"""
import re
from datetime import timedelta
from zoneinfo import ZoneInfo, available_timezones

import discord

from . import events, handlers, openings
from .db import DuplicateRun, parse_iso
from .event_form import _input, parse_range
from .grid import parse_local_datetime
from .services import cutoff_of, day_label, day_state
from .snapshots import ensure_snapshot
from .views import RunPickerView

MAX_NAME = 60


# ---------------------------------------------------------------- the actions (text in, text out)

async def open_event(ctx, run):
    """Open a draft. Returns (ok, text)."""
    return await openings.open_run(ctx, run["guild_id"], run)


def close_event(ctx, run):
    """Close signups for every remaining day and the event itself."""
    if run["status"] != "open":
        return f"**{run['name']}** is not open."
    now = ctx.now()
    for row in ctx.db.run_days(run["id"]):
        if now < cutoff_of(row):
            ctx.db.set_cutoff(run["id"], row["day"], now)
            ctx.db.take_snapshot(run["id"], row["day"], ctx.cfg, now)
        else:
            ensure_snapshot(ctx, run, row["day"])     # closed earlier but the loop has not got to it
    ctx.db.set_run_status(run["id"], "closed")
    n = len({r["discord_id"] for r in ctx.db.tick_rows(run["id"])})
    return (f"**{run['name']}** is closed for every day, with {n} players signed up. "
            "Check /run status, then /draft export for each day." if run["mode"] == "roles" else
            f"**{run['name']}** is closed for every day, with {n} people signed up. Check /run status for the result.")


def close_day(ctx, run, row):
    """Close one day's signups now."""
    if run["status"] != "open":
        return f"**{run['name']}** is not open."
    now = ctx.now()
    if now >= cutoff_of(row):
        return f"{day_label(row['day'])} is already closed."
    ctx.db.set_cutoff(run["id"], row["day"], now)
    ctx.db.take_snapshot(run["id"], row["day"], ctx.cfg, now)
    if run["mode"] == "availability":
        return f"Signups for {day_label(row['day'])} are closed now."
    return (f"Signups for {day_label(row['day'])} are closed now and their roles, prog and limits are saved. "
            f"Next: `/draft export day:{row['day']}`.")


def move_cutoff(ctx, run, row, new_cutoff, timezone=None):
    """Move one day's cutoff (this also reopens a day that already closed, and a closed event)."""
    tz = timezone.strip() if timezone and timezone.strip() in available_timezones() else ctx.cfg["timezone"]
    try:
        cutoff = parse_local_datetime(new_cutoff, tz)
    except ValueError:
        return (f"The new cutoff looks like 2026-11-01 23:59 (time in {tz}). "
                "You can also use today and tomorrow with a time.")
    first = ctx.db.first_slot_start(run["id"], row["day"])
    if cutoff >= first:
        return (f"The cutoff must be before {day_label(row['day'])}'s first slot "
                f"({discord.utils.format_dt(first, 'F')}).")
    if cutoff <= ctx.now():
        return "That time has already passed. To close a day now use Close this day."
    if run["status"] == "closed":
        ctx.db.set_run_status(run["id"], "open")
    was_exported = bool(row["exported_utc"])
    ctx.db.set_cutoff(run["id"], row["day"], cutoff, reset_export=True)
    ctx.db.clear_snapshot(run["id"], row["day"])         # the day reopens; a new snapshot is taken at the new cutoff
    note = (" It had been exported, so export again after the new cutoff; the stored draft for that day stays "
            "until you import a new one." if was_exported else "")
    if row["snapshot_utc"]:
        note += " The signups saved at the old cutoff were dropped; a new snapshot is taken at the new cutoff."
    return f"Signups for {day_label(row['day'])} now close {discord.utils.format_dt(cutoff, 'F')}." + note


def edit_details(ctx, run, name, party_size):
    """Rename the event and/or change an availability event's group size. Returns (ok, text)."""
    name = (name or "").strip()
    if not name or len(name) > MAX_NAME:
        return False, f"The name must be 1 to {MAX_NAME} characters."
    other = ctx.db.get_run(run["guild_id"], name)
    if other is not None and other["id"] != run["id"]:
        return False, f"There is already an event called **{other['name']}**."
    size = run["party_size"]
    if run["mode"] == "availability":
        text = (party_size or "").strip()
        if text:
            if not text.isdigit() or not 1 <= int(text) <= events.MAX_PARTY_SIZE:
                return False, f"The group size must be a whole number from 1 to {events.MAX_PARTY_SIZE}."
            size = int(text)
    try:
        ctx.db.update_run_details(run["id"], name, size)
    except DuplicateRun:
        return False, f"There is already an event called **{name}**."
    return True, "Saved."


def parse_added_days(text):
    """'2026-11-09 to 2026-11-11, 2026-11-14' -> sorted unique ISO dates. Raises ValueError with a reason."""
    found = set()
    for part in re.split(r"[,;\n]+", text or ""):
        if not part.strip():
            continue
        first, last = parse_range(part)
        if (last - first).days > 60:
            raise ValueError("That range is too long.")
        found.update((first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1))
    return sorted(found)


def change_days(ctx, run, add_text, remove):
    """Add days (new days copy the time window of the event's first day and get the default cutoff) and/or remove
    days (refused for a day that was exported or has parties). Returns (ok, text)."""
    try:
        added = parse_added_days(add_text)
    except ValueError as e:
        return False, f"Add days: {e or 'use 2026-11-09, or 2026-11-09 to 2026-11-11, separated by commas.'}"
    current = [r["day"] for r in ctx.db.run_days(run["id"])]
    added = [d for d in added if d not in current]
    remove = [d for d in remove if d in current]
    if not added and not remove:
        return False, "Nothing to change: no new days, and no days picked to remove."
    for day in remove:
        why = ctx.db.day_blockers(run["id"], day)
        if why:
            return False, f"{day_label(day)} cannot be removed: {' and '.join(why)}. Nothing was changed."
    remaining = [d for d in current if d not in remove]
    if not remaining and not added:
        return False, "An event needs at least one day. Delete the event instead."
    if len(remaining) + len(added) > ctx.cfg["max_run_days"]:
        return False, f"An event can have at most {ctx.cfg['max_run_days']} days. Nothing was changed."
    spec = events.spec_from_run(ctx.db, run, ctx.cfg, ctx.grid)
    plan = None
    if added:
        template = {k: v for k, v in (spec["days"][0] if spec.get("days") else {}).items()
                    if k in ("first_start", "last_start", "slot_minutes")}
        new_spec = {k: v for k, v in spec.items() if k not in ("days", "announce")}
        new_spec.update(template, days=[{"date": d} for d in added])
        plan, errors = events.resolve(new_spec, ctx.cfg, ctx.grid, ctx.db, ctx.now())
        if errors:
            return False, "Could not add those days:\n" + "\n".join(f"• {e}" for e in errors[:6])
    dropped = ctx.db.remove_days(run["id"], remove) if remove else 0
    if plan:
        ctx.db.add_days(run["id"], plan.slots, plan.cutoffs)
    if run["spec"]:                                   # keep the stored spec in step with the days
        kept = [d for d in spec.get("days", []) if d.get("date") in remaining or d.get("date") not in current]
        spec["days"] = sorted(kept + (plan.spec["days"] if plan else []), key=lambda d: d["date"])
        ctx.db.set_spec(run["id"], spec)
    bits = []
    if added:
        bits.append(f"added {len(added)} day(s)")
    if remove:
        bits.append(f"removed {len(remove)} day(s)" + (f" and dropped {dropped} signed-up slot(s)" if dropped else ""))
    note = ""
    if added and run["status"] == "closed":
        note = " The event is closed: move one of the new days' cutoffs to reopen it."
    return True, "Done: " + " and ".join(bits) + "." + note


def delete_event(ctx, run):
    name = run["name"]
    ctx.db.delete_run(run["id"])
    return (f"**{name}** was deleted. Its announcement and any party threads in Discord are still there; "
            "delete them by hand if you want them gone.")


# ---------------------------------------------------------------- the panel

def render(ctx, run, selected, notice=""):
    kind = (f"availability only, groups of {run['party_size']}" if run["mode"] == "availability"
            else (ctx.db.fight(run["fight_id"]) or {"name": "?"})["name"])
    lines = [f"**{run['name']}** · {run['status']} · {kind}"]
    if run["open_at_utc"]:
        lines.append(f"Opens by itself {discord.utils.format_dt(parse_iso(run['open_at_utc']), 'F')}.")
    if run["announce_message_id"]:
        lines.append("Announced.")
    for row in ctx.db.run_days(run["id"]):
        mark = "▶ " if row["day"] == selected else "• "
        lines.append(f"{mark}{day_label(row['day'])}: {_day_state(ctx, run, row)}")
    if notice:
        lines += ["", notice]
    return "\n".join(lines)[:1990]


def _day_state(ctx, run, row):
    state = day_state(ctx, run, row)
    cutoff = discord.utils.format_dt(cutoff_of(row), "R")
    return {"open": f"open, closes {cutoff}", "draft": f"not open yet, would close {cutoff}",
            "closed": f"closed {cutoff}", "exported": f"exported (cutoff {cutoff})"}[state]


class RunEditView(discord.ui.View):
    def __init__(self, ctx, run_id, user_id):
        super().__init__(timeout=900)
        self.ctx, self.run_id, self.user_id = ctx, run_id, user_id
        self.notice = ""
        rows = ctx.db.run_days(run_id)
        run = ctx.db.run(run_id)
        self.day = next((r["day"] for r in rows if day_state(ctx, run, r) == "open"), rows[0]["day"])
        self.day_select = discord.ui.Select(placeholder="Pick a day", row=0, min_values=1, max_values=1,
                                            options=[discord.SelectOption(label=day_label(r["day"]), value=r["day"])
                                                     for r in rows[:25]])
        self.day_select.callback = self.day_picked
        self.toggle = discord.ui.Button(label="Open event", style=discord.ButtonStyle.success, row=1)
        self.toggle.callback = self.toggle_event
        self.close_day_button = discord.ui.Button(label="Close this day now", style=discord.ButtonStyle.danger, row=1)
        self.close_day_button.callback = self.close_this_day
        self.cutoff_button = discord.ui.Button(label="Change cutoff", style=discord.ButtonStyle.secondary, row=2)
        self.cutoff_button.callback = self.change_cutoff
        self.details_button = discord.ui.Button(label="Edit details", style=discord.ButtonStyle.secondary, row=2)
        self.details_button.callback = self.edit_details_modal
        self.days_button = discord.ui.Button(label="Change days", style=discord.ButtonStyle.secondary, row=2)
        self.days_button.callback = self.change_days_modal
        self.delete_button = discord.ui.Button(label="Delete event", style=discord.ButtonStyle.danger, row=3)
        self.delete_button.callback = self.ask_delete
        for item in (self.day_select, self.toggle, self.close_day_button, self.cutoff_button, self.days_button,
                     self.details_button, self.delete_button):
            self.add_item(item)
        self.sync()

    @property
    def run(self):
        return self.ctx.db.run(self.run_id)

    def row(self):
        return self.ctx.db.run_day(self.run_id, self.day)

    def refresh_days(self):
        """Rebuild the day dropdown after days were added or removed."""
        rows = self.ctx.db.run_days(self.run_id)
        self.day_select.options = [discord.SelectOption(label=day_label(r["day"]), value=r["day"]) for r in rows[:25]]

    def sync(self):
        run, ctx = self.run, self.ctx
        for opt in self.day_select.options:
            opt.default = opt.value == self.day
            row = ctx.db.run_day(self.run_id, opt.value)
            opt.description = _day_state_plain(ctx, run, row)
        status = run["status"]
        self.toggle.label = {"draft": "Open event", "open": "Close event", "closed": "Event closed"}[status]
        self.toggle.style = (discord.ButtonStyle.danger if status == "open" else discord.ButtonStyle.success)
        self.toggle.disabled = status == "closed"                  # reopen a closed event by moving a day's cutoff
        self.close_day_button.disabled = not (status == "open" and day_state(ctx, run, self.row()) == "open")

    def text(self):
        return render(self.ctx, self.run, self.day, self.notice)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This panel is not yours. Use /run edit.", ephemeral=True)
            return False
        return True

    async def redraw(self, interaction, notice=""):
        self.notice = notice
        self.sync()
        await interaction.response.edit_message(content=self.text(), view=self)

    async def day_picked(self, interaction):
        self.day = self.day_select.values[0]
        await self.redraw(interaction)

    async def toggle_event(self, interaction):
        run = self.run
        if run["status"] == "draft":
            await interaction.response.defer()                    # posting the announcement can take a moment
            _, text = await open_event(self.ctx, run)
            self.notice = text
            self.sync()
            return await interaction.edit_original_response(content=self.text(), view=self)
        await self.redraw(interaction, close_event(self.ctx, run))

    async def close_this_day(self, interaction):
        await self.redraw(interaction, close_day(self.ctx, self.run, self.row()))

    async def change_cutoff(self, interaction):
        await interaction.response.send_modal(CutoffModal(self))

    async def edit_details_modal(self, interaction):
        await interaction.response.send_modal(DetailsModal(self))

    async def change_days_modal(self, interaction):
        await interaction.response.send_modal(DaysModal(self))

    async def ask_delete(self, interaction):
        confirm = ConfirmDeleteView(self)
        await interaction.response.edit_message(content=confirm.text(), view=confirm)


def _day_state_plain(ctx, run, row):
    """Plain text for a dropdown (Discord timestamps do not render inside dropdowns)."""
    state = day_state(ctx, run, row)
    local = cutoff_of(row).astimezone(ZoneInfo(ctx.grid.tz)).strftime("%a %d %b %H:%M")
    return {"open": f"open, closes {local}", "draft": f"not open yet, closes {local}",
            "closed": f"closed {local}", "exported": f"exported"}[state][:100]


class CutoffModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title=f"Cutoff of {day_label(view.day)}"[:45])
        self.owner = view
        ctx = view.ctx
        current = cutoff_of(view.row()).astimezone(ZoneInfo(ctx.grid.tz)).strftime("%Y-%m-%d %H:%M")
        self.cutoff = _input("New cutoff", current, "2026-11-01 23:59", required=True, max_length=16)
        self.tz = _input("Timezone", "", ctx.cfg["timezone"], max_length=40)
        self.add_item(discord.ui.Label(text="Signups close at", component=self.cutoff,
                                       description="Moving it into the future also reopens a closed day."))
        self.add_item(discord.ui.Label(text="Timezone", component=self.tz,
                                       description=f"Empty = {ctx.cfg['timezone']}."))

    async def on_submit(self, interaction):
        v = self.owner
        await v.redraw(interaction, move_cutoff(v.ctx, v.run, v.row(), self.cutoff.value, self.tz.value))


class DetailsModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Event details")
        self.owner = view
        run = view.run
        self.name = _input("Name", run["name"], "UWU week 1", required=True, max_length=MAX_NAME)
        self.add_item(discord.ui.Label(text="Name", component=self.name))
        self.size = None
        if run["mode"] == "availability":
            self.size = _input("Group size", str(run["party_size"] or ""), "8", max_length=3)
            self.add_item(discord.ui.Label(text="Group size", component=self.size,
                                           description="How many people make a group."))

    async def on_submit(self, interaction):
        v = self.owner
        ok, text = edit_details(v.ctx, v.run, self.name.value, self.size.value if self.size else None)
        await v.redraw(interaction, text if not ok else "Saved.")


class DaysModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Change the days")
        self.owner = view
        ctx = view.ctx
        self.add = _input("Add days", "", "2026-11-09 to 2026-11-11, 2026-11-14", max_length=200)
        rows = ctx.db.run_days(view.run_id)[:25]
        self.remove = discord.ui.Select(
            min_values=0, max_values=len(rows), required=False,
            options=[discord.SelectOption(label=day_label(r["day"]), value=r["day"]) for r in rows])
        self.add_item(discord.ui.Label(text="Add days", component=self.add,
                                       description="Dates or ranges, separated by commas. They get the first day's "
                                                   "times and the default cutoff."))
        self.add_item(discord.ui.Label(text="Remove days", component=self.remove,
                                       description="Not possible for a day that was exported or has parties."))

    async def on_submit(self, interaction):
        v = self.owner
        ok, text = change_days(v.ctx, v.run, self.add.value, list(self.remove.values))
        if ok:
            rows = v.ctx.db.run_days(v.run_id)
            if v.day not in {r["day"] for r in rows}:
                v.day = rows[0]["day"]
            v.refresh_days()
        await v.redraw(interaction, text)


class ConfirmDeleteView(discord.ui.View):
    """Deleting is permanent, so it is its own step."""

    def __init__(self, owner):
        super().__init__(timeout=300)
        self.owner = owner
        yes = discord.ui.Button(label="Yes, delete it", style=discord.ButtonStyle.danger)
        yes.callback = self.confirm
        no = discord.ui.Button(label="Keep it", style=discord.ButtonStyle.secondary)
        no.callback = self.cancel
        self.add_item(yes)
        self.add_item(no)

    def text(self):
        ctx, run = self.owner.ctx, self.owner.run
        players, parties = ctx.db.run_counts(run["id"])
        return (f"Delete **{run['name']}** for good? This removes its {len(ctx.db.run_days(run['id']))} days, "
                f"{players} signed-up players' signups and {parties} parties, including those parties' ratings and "
                "feedback. It cannot be undone.")

    async def interaction_check(self, interaction):
        return await self.owner.interaction_check(interaction)

    async def confirm(self, interaction):
        text = delete_event(self.owner.ctx, self.owner.run)
        self.stop()
        self.owner.stop()
        await interaction.response.edit_message(content=text, view=None)

    async def cancel(self, interaction):
        self.stop()
        await self.owner.redraw(interaction)


# ---------------------------------------------------------------- /run edit

async def start(ctx, interaction, name=None):
    if not await handlers.require_organizer(ctx, interaction):
        return
    if not name:
        live = ctx.db.live_runs(interaction.guild_id)
        if len(live) > 1:
            async def picked(pick_interaction, run):
                view = RunEditView(ctx, run["id"], pick_interaction.user.id)
                await pick_interaction.response.edit_message(content=view.text(), view=view)
            picker = RunPickerView(live, interaction.user.id, picked)
            return await interaction.response.send_message("Several events are live. Which one?", view=picker,
                                                           ephemeral=True)
    run = ctx.db.get_run(interaction.guild_id, name)
    if run is None:
        return await handlers.say(interaction, f"No run called '{name}' in this server." if name
                                  else "There are no runs yet. Use /run create.")
    view = RunEditView(ctx, run["id"], interaction.user.id)
    await interaction.response.send_message(view.text(), view=view, ephemeral=True)
