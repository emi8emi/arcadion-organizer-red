"""The guided event form: /run create without a file.

One private message with a live preview and buttons. Nothing is saved until Create; the form only builds an
event spec (bot/events.py) and hands it to `handlers.submit_event`, the same path an uploaded JSON file takes,
so the two can never disagree. The preview is `events.resolve` run on every change: problems show up as soon
as they exist and Create stays disabled until there are none.
"""
import re
from dataclasses import dataclass, field
from datetime import timedelta

import discord

from . import events, handlers
from .grid import parse_date

MAX_DAY_OPTIONS = 25
DAY_FIELDS = ("first_start", "last_start", "slot_minutes", "cutoff_at")


@dataclass
class EventDraft:
    name: str = ""
    content: str = ""
    mode: str = "roles"                  # "roles" or "availability" (only collect who can make which slot)
    party_size: str = ""                 # group size of an availability event; "" = the default (8)
    first: object = None                 # the date range as entered; `days` is what is ticked inside it
    last: object = None
    days: list = field(default_factory=list)          # ISO dates
    first_start: str = ""                # defaults for all days; "" = the bot's config
    last_start: str = ""
    slot_minutes: str = ""
    cutoff_time: str = ""
    days_before: str = ""
    cutoff_timezone: str = ""
    channel_id: str = ""
    role_id: str = ""
    message: str = ""
    open_at: str = ""
    overrides: dict = field(default_factory=dict)     # ISO date -> {first_start, last_start, slot_minutes, cutoff_at}

    def range_days(self):
        if self.first is None:
            return []
        return [(self.first + timedelta(days=n)).isoformat() for n in range((self.last - self.first).days + 1)]

    def ready(self):
        return bool(self.name and (self.content or self.mode == "availability") and self.days)

    def to_spec(self):
        spec = {"version": events.SPEC_VERSION, "name": self.name}
        if self.content and self.mode == "roles":
            spec["content"] = self.content
        if self.mode == "availability":
            spec["mode"] = "availability"
            if self.party_size:
                spec["party_size"] = int(self.party_size) if self.party_size.isdigit() else self.party_size
        for key in ("first_start", "last_start", "cutoff_timezone"):
            if getattr(self, key):
                spec[key] = getattr(self, key)
        if self.slot_minutes:
            spec["slot_minutes"] = int(self.slot_minutes) if self.slot_minutes.isdigit() else self.slot_minutes
        cutoff = {}
        if self.cutoff_time:
            cutoff["time"] = self.cutoff_time
        if self.days_before != "":
            cutoff["days_before"] = int(self.days_before) if self.days_before.isdigit() else self.days_before
        if cutoff:
            spec["cutoff"] = cutoff
        spec["days"] = [dict(self.overrides[d], date=d) if self.overrides.get(d) else d for d in self.days]
        announce = {k: v for k, v in (("channel_id", self.channel_id), ("ping_role_id", self.role_id),
                                      ("message", self.message), ("open_at", self.open_at)) if v}
        if announce:
            spec["announce"] = announce
        return spec


def parse_range(text):
    """'2026-11-02 to 2026-11-08', '2026-11-02' or 'today to tomorrow' -> (first, last). Raises ValueError."""
    parts = re.split(r"\s+(?:to|until)\s+|\s*(?:–|—|\.\.)\s*", text.strip(), maxsplit=1)
    first = parse_date(parts[0])
    last = parse_date(parts[1]) if len(parts) > 1 and parts[1].strip() else first
    if last < first:
        raise ValueError("The last day is before the first day.")
    return first, last


def _input(label, value="", placeholder=None, required=False, max_length=40, paragraph=False):
    """A text box for use inside a discord.ui.Label. Discord refuses a label on the TextInput itself (the Label
    holds the text), so `label` is only kept as the first argument to read well at the call site."""
    return discord.ui.TextInput(default=value or None, placeholder=placeholder, required=required,
                                max_length=max_length,
                                style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short)


class EventFormView(discord.ui.View):
    def __init__(self, ctx, user_id):
        super().__init__(timeout=1800)
        self.ctx, self.user_id = ctx, user_id
        self.draft, self.notice = EventDraft(), ""
        self.days_select = discord.ui.Select(placeholder="Days of the event", min_values=1, row=0,
                                             options=[discord.SelectOption(label="Set the dates first", value="none")],
                                             disabled=True)
        self.days_select.callback = self.days_chosen
        self.day_edit = discord.ui.Select(placeholder="Change one day (its times or cutoff)", min_values=1, row=1,
                                          options=[discord.SelectOption(label="Pick days first", value="none")],
                                          disabled=True)
        self.day_edit.callback = self.day_picked
        self.add_item(self.days_select)
        self.add_item(self.day_edit)
        for label, modal in (("Basics", BasicsModal), ("Times", TimesModal), ("Cutoff", CutoffModal),
                             ("Announce", AnnounceModal)):
            self.add_item(FormButton(label, modal, row=2))
        self.create_button = discord.ui.Button(label="Create event", style=discord.ButtonStyle.success, row=3,
                                               disabled=True)
        self.create_button.callback = self.create
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
        cancel.callback = self.cancel
        self.add_item(self.create_button)
        self.add_item(cancel)
        self.plan, self.errors = None, []
        self.sync()

    def _day_label(self, iso):
        return parse_date(iso).strftime("%a %d %b")

    def sync(self):
        d = self.draft
        in_range = d.range_days()
        if in_range:
            self.days_select.disabled = False
            self.days_select.max_values = min(len(in_range), MAX_DAY_OPTIONS)
            self.days_select.options = [discord.SelectOption(label=self._day_label(day), value=day,
                                                             default=day in d.days)
                                        for day in in_range[:MAX_DAY_OPTIONS]]
        if d.days:
            self.day_edit.disabled = False
            self.day_edit.options = [discord.SelectOption(label=self._day_label(day) + (" ✎" if d.overrides.get(day) else ""),
                                                          value=day) for day in d.days[:MAX_DAY_OPTIONS]]
        else:
            self.day_edit.disabled = True
        self.plan, self.errors = (None, [])
        if d.ready():
            self.plan, self.errors = events.resolve(d.to_spec(), self.ctx.cfg, self.ctx.grid, self.ctx.db, self.ctx.now())
        self.create_button.disabled = self.plan is None

    def render(self):
        d = self.draft
        head = "**New event**"
        if not d.ready():
            body = ("Press **Basics** to set the name, the type, the fight and the dates. Then tick the days, and change times, "
                    "the signup cutoff or the announcement if the defaults are not right. Nothing is saved until you "
                    "press **Create event**.")
        elif self.errors:
            body = "⚠ Fix these before creating it:\n" + "\n".join(f"• {e}" for e in self.errors[:8])
        else:
            body = events.describe(self.plan)
        text = "\n".join(x for x in (head, self.notice, body) if x)
        return text[:1990]

    async def interaction_check(self, interaction):
        return interaction.user.id == self.user_id

    async def redraw(self, interaction):
        self.sync()
        await interaction.response.edit_message(content=self.render(), view=self)

    async def days_chosen(self, interaction):
        chosen = sorted(self.days_select.values)
        self.draft.days = chosen
        self.draft.overrides = {k: v for k, v in self.draft.overrides.items() if k in chosen}
        self.notice = ""
        await self.redraw(interaction)

    async def day_picked(self, interaction):
        await interaction.response.send_modal(DayModal(self, self.day_edit.values[0]))

    async def create(self, interaction):
        await interaction.response.defer()
        if await handlers.submit_event(self.ctx, interaction, self.draft.to_spec()):
            self.stop()
            await interaction.edit_original_response(content="✅ Event created. Details below.", view=None)

    async def cancel(self, interaction):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was created.", view=None)


class FormButton(discord.ui.Button):
    def __init__(self, label, modal, row):
        super().__init__(label=label, style=discord.ButtonStyle.primary if label == "Basics" else discord.ButtonStyle.secondary,
                         row=row)
        self.modal = modal

    async def callback(self, interaction):
        await interaction.response.send_modal(self.modal(self.view))


class BasicsModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Name, fight and dates")
        self.owner = view
        d = view.draft
        self.name = _input("Name", d.name, "UWU week 1", required=True, max_length=60)
        fights = view.ctx.db.fights()[:25]
        self.content = discord.ui.Select(
            min_values=0, max_values=1, required=False,
            options=[discord.SelectOption(label=f["name"][:100], value=f["name"][:100], default=f["name"] == d.content)
                     for f in fights] or [discord.SelectOption(label="No fights known", value="none")])
        shown = "" if d.first is None else (d.first.isoformat() if d.first == d.last
                                            else f"{d.first.isoformat()} to {d.last.isoformat()}")
        self.dates = _input("Dates", shown, "2026-11-02 to 2026-11-08", required=True, max_length=60)
        self.kind = discord.ui.Select(
            min_values=1, max_values=1,
            options=[discord.SelectOption(label="Raid parties with roles", value="roles", default=d.mode == "roles",
                                          description="The bot builds role-balanced parties for the fight."),
                     discord.SelectOption(label="Availability only", value="availability",
                                          default=d.mode == "availability",
                                          description="Just collect who can make which slot (like when2meet).")])
        self.size = _input("Group size", d.party_size, "8", max_length=3)
        self.add_item(discord.ui.Label(text="Name", component=self.name))
        self.add_item(discord.ui.Label(text="Type of event", component=self.kind))
        self.add_item(discord.ui.Label(text="Fight", component=self.content,
                                       description="Raid parties only; leave empty for availability only."))
        self.add_item(discord.ui.Label(text="Dates", component=self.dates,
                                       description="One day, or first to last day. You can untick days afterwards."))
        self.add_item(discord.ui.Label(text="Group size", component=self.size,
                                       description="Availability only: the group size you look for. Empty = 8."))

    async def on_submit(self, interaction):
        v, d = self.owner, self.owner.draft
        d.name = self.name.value.strip()
        d.content = self.content.values[0] if self.content.values and self.content.values[0] != "none" else ""
        d.mode = self.kind.values[0] if self.kind.values else "roles"
        d.party_size = self.size.value.strip()
        try:
            first, last = parse_range(self.dates.value)
        except ValueError as e:
            v.notice = f"⚠ Dates: {e or 'use 2026-11-02 to 2026-11-08.'} The dates were not changed."
            return await v.redraw(interaction)
        limit = min(v.ctx.cfg["max_run_days"], MAX_DAY_OPTIONS)
        if (last - first).days + 1 > limit:
            v.notice = f"⚠ An event can be at most {limit} days long. The dates were not changed."
            return await v.redraw(interaction)
        if (first, last) != (d.first, d.last):
            d.first, d.last = first, last
            d.days = d.range_days()                                   # a new range starts with every day ticked
            d.overrides = {}
        v.notice = ""
        await v.redraw(interaction)


class TimesModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Slot times for every day")
        self.owner = view
        d, g = view.draft, view.ctx.grid
        self.first = _input("First slot starts at", d.first_start, f"{g.day_start}", max_length=5)
        self.last = _input("Last slot starts at or before", d.last_start, f"{g.last_start}", max_length=5)
        self.minutes = _input("Slot length in minutes", d.slot_minutes, f"{g.slot_minutes}", max_length=3)
        for text, item in (("First start", self.first), ("Last start", self.last), ("Slot length", self.minutes)):
            self.add_item(discord.ui.Label(text=text, component=item,
                                           description="Leave empty for the default." if text == "First start" else None))

    async def on_submit(self, interaction):
        d = self.owner.draft
        d.first_start, d.last_start, d.slot_minutes = (self.first.value.strip(), self.last.value.strip(),
                                                       self.minutes.value.strip())
        self.owner.notice = ""
        await self.owner.redraw(interaction)


class CutoffModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="When signups close")
        self.owner = view
        d, c = view.draft, view.ctx.cfg
        self.time = _input("Cutoff time", d.cutoff_time, c["cutoff_time"], max_length=5)
        self.before = _input("Days before each day", d.days_before, str(c["cutoff_days_before"]), max_length=1)
        self.tz = _input("Timezone", d.cutoff_timezone, c["timezone"], max_length=40)
        self.add_item(discord.ui.Label(text="Cutoff time", component=self.time, description="Like 23:59. Empty = default."))
        self.add_item(discord.ui.Label(text="Days before", component=self.before,
                                       description="1 = the evening before, 0 = the day itself."))
        self.add_item(discord.ui.Label(text="Timezone of the cutoff", component=self.tz))

    async def on_submit(self, interaction):
        d = self.owner.draft
        d.cutoff_time, d.days_before, d.cutoff_timezone = (self.time.value.strip(), self.before.value.strip(),
                                                           self.tz.value.strip())
        self.owner.notice = ""
        await self.owner.redraw(interaction)


class DayModal(discord.ui.Modal):
    """One day that differs from the rest: its own slot window, slot length or exact cutoff."""

    def __init__(self, view, day):
        super().__init__(title=f"Change {view._day_label(day)}")
        self.owner, self.day = view, day
        cur = view.draft.overrides.get(day, {})
        d, g = view.draft, view.ctx.grid
        self.items = {
            "first_start": _input("First slot starts at", cur.get("first_start", ""), d.first_start or g.day_start, max_length=5),
            "last_start": _input("Last slot starts at or before", cur.get("last_start", ""), d.last_start or g.last_start, max_length=5),
            "slot_minutes": _input("Slot length in minutes", str(cur.get("slot_minutes", "")), d.slot_minutes or str(g.slot_minutes), max_length=3),
            "cutoff_at": _input("Signups close at", cur.get("cutoff_at", ""), "2026-11-03 18:00", max_length=16),
        }
        labels = {"first_start": "First start", "last_start": "Last start", "slot_minutes": "Slot length",
                  "cutoff_at": "Cutoff (date and time)"}
        for key, item in self.items.items():
            self.add_item(discord.ui.Label(text=labels[key], component=item,
                                           description="Empty = same as the other days." if key == "first_start" else None))

    async def on_submit(self, interaction):
        v = self.owner
        values = {k: i.value.strip() for k, i in self.items.items() if i.value.strip()}
        if "slot_minutes" in values and values["slot_minutes"].isdigit():
            values["slot_minutes"] = int(values["slot_minutes"])
        if values:
            v.draft.overrides[self.day] = values
        else:
            v.draft.overrides.pop(self.day, None)
        v.notice = ""
        await v.redraw(interaction)


class AnnounceModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Announcement and opening")
        self.owner = view
        d = view.draft
        self.channel = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text], min_values=0, max_values=1,
                                                required=False)
        self.role = discord.ui.RoleSelect(min_values=0, max_values=1, required=False)
        self.message = _input("Message", d.message, "Anything players should know", max_length=events.MAX_MESSAGE,
                              paragraph=True)
        self.open_at = _input("Open signups at", d.open_at, "2026-11-01 12:00", max_length=16)
        self.add_item(discord.ui.Label(text="Channel to announce in", component=self.channel,
                                       description="Leave empty for no announcement. Pick again to change it."))
        self.add_item(discord.ui.Label(text="Role to ping", component=self.role))
        self.add_item(discord.ui.Label(text="Message", component=self.message))
        self.add_item(discord.ui.Label(text="Open signups at", component=self.open_at,
                                       description="Date and time in the cutoff timezone. Empty = you open it with /run edit."))

    async def on_submit(self, interaction):
        d = self.owner.draft
        if self.channel.values:
            d.channel_id = str(self.channel.values[0].id)
        if self.role.values:
            d.role_id = str(self.role.values[0].id)
        d.message, d.open_at = self.message.value.strip(), self.open_at.value.strip()
        self.owner.notice = ""
        await self.owner.redraw(interaction)


async def start(ctx, interaction):
    view = EventFormView(ctx, interaction.user.id)
    await interaction.response.send_message(view.render(), view=view, ephemeral=True)
