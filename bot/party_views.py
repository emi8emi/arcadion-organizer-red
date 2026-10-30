"""Buttons on a party's message in its thread: "Party ended" (any member, organizers can override or undo),
"Edit composition" (organizers), and the "Why was I swapped?" button on the swap notice.

The buttons are DynamicItems: the party (or change) id is in the custom_id, so they work after a restart.
The logic lives in plain async functions that take `ctx`, so tests can call them with fake interactions.
"""
import io
import logging

import discord

from .config import ROLES
from .db import parse_iso
from .parties import (REASONS, apply_swap, comp_warnings, dumps, minutes_played, name_of, party_json,
                      post_party_rating_prompt, reason_label, refresh_party_message, swap_warnings)

log = logging.getLogger("raidbot")


def _is_organizer(ctx, user):
    from .handlers import is_organizer
    return is_organizer(ctx, user)


async def _reply(interaction, text, **kwargs):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(text, ephemeral=True, **kwargs)


# ---------------------------------------------------------------- the buttons under a party message

class PartyEndedButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pend:(?P<party>[0-9]+)"):
    def __init__(self, party_id):
        super().__init__(discord.ui.Button(label="Party ended", style=discord.ButtonStyle.success,
                                           custom_id=f"pend:{party_id}"))
        self.party_id = int(party_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["party"]))

    async def callback(self, interaction):
        await press_end(interaction.client.ctx, interaction, self.party_id)


class PartyEditButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pedit:(?P<party>[0-9]+)"):
    def __init__(self, party_id):
        super().__init__(discord.ui.Button(label="Edit composition", style=discord.ButtonStyle.secondary,
                                           custom_id=f"pedit:{party_id}"))
        self.party_id = int(party_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["party"]))

    async def callback(self, interaction):
        await open_edit_panel(interaction.client.ctx, interaction, self.party_id)


class PartyActionsView(discord.ui.View):
    def __init__(self, party_id):
        super().__init__(timeout=None)
        self.add_item(PartyEndedButton(party_id))
        self.add_item(PartyEditButton(party_id))


class WhySwappedButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pwhy:(?P<change>[0-9]+)"):
    def __init__(self, change_id):
        super().__init__(discord.ui.Button(label="Why was I swapped?", style=discord.ButtonStyle.primary,
                                           custom_id=f"pwhy:{change_id}"))
        self.change_id = int(change_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["change"]))

    async def callback(self, interaction):
        await show_reason(interaction.client.ctx, interaction, self.change_id)


class WhySwappedView(discord.ui.View):
    def __init__(self, change_id):
        super().__init__(timeout=None)
        self.add_item(WhySwappedButton(change_id))


async def show_reason(ctx, interaction, change_id):
    """Ephemeral: the reason an organizer gave. Only the swapped player (and organizers) can read it."""
    change = ctx.db.change(change_id)
    me = str(interaction.user.id)
    if change is None or change["kind"] != "swap" or not (me == change["player_id"] or _is_organizer(ctx, interaction.user)):
        return await _reply(interaction, "This notice is not about you.")
    await _reply(interaction, f"You were swapped out by <@{change['actor_id']}>.\n"
                              f"**Reason:** {reason_label(change['reason_code'], change['reason_text'])}")


# ---------------------------------------------------------------- party ended

async def press_end(ctx, interaction, party_id):
    party = ctx.db.party(party_id)
    me = str(interaction.user.id)
    if party is None or party["status"] not in ("confirmed", "done"):
        return await _reply(interaction, "That party is not active.")
    member = me in {m["discord_id"] for m in ctx.db.party_members(party_id)}
    organizer = _is_organizer(ctx, interaction.user)
    if not (member or organizer):
        return await _reply(interaction, "Only the players in this party (or organizers) can end it.")
    start = parse_iso(party["start_utc"])
    if party["ended_utc"]:
        when = discord.utils.format_dt(parse_iso(party["ended_utc"]), "t")
        if organizer:
            view = UndoEndView(ctx, party_id, me)
            return await _reply(interaction, f"This party was marked ended at {when}. Undo it? Play time is removed "
                                             "and it counts as running again until its slot ends.", view=view)
        return await _reply(interaction, f"This party was already marked ended at {when}.")
    if ctx.now() < start:
        return await _reply(interaction, f"The slot has not started yet: it starts {discord.utils.format_dt(start, 'R')}.")
    override = organizer and not member
    view = EndConfirmView(ctx, party_id, me, override)
    what = "End it for everyone (organizer override)?" if override else "Mark it as ended?"
    await _reply(interaction, f"**Party {party['party_index'] + 1}**: {what} This opens the ratings for all of "
                              "you and stops edits being needed. Only press Yes if you really finished.", view=view)


class EndConfirmView(discord.ui.View):
    def __init__(self, ctx, party_id, user_id, override):
        super().__init__(timeout=120)
        self.ctx, self.party_id, self.user_id, self.override = ctx, party_id, str(user_id), override

    async def interaction_check(self, interaction):
        return str(interaction.user.id) == self.user_id

    @discord.ui.button(label="Yes, it ended", style=discord.ButtonStyle.success)
    async def yes(self, interaction, button):
        self.stop()
        await confirm_end(self.ctx, interaction, self.party_id, self.override)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing changed.", view=None)


async def confirm_end(ctx, interaction, party_id, override):
    now = ctx.now()
    party = ctx.db.party(party_id)
    if party is None or party["ended_utc"]:
        return await interaction.response.edit_message(content="Someone already ended this party.", view=None)
    if now < parse_iso(party["start_utc"]):
        return await interaction.response.edit_message(content="The slot has not started yet.", view=None)
    ctx.db.end_party(party_id, now, interaction.user.id, minutes_played(party, now),
                     actor_id=interaction.user.id, override=override)
    await interaction.response.edit_message(content="✅ Marked as ended. The rating button is in the thread.", view=None)
    await refresh_party_message(ctx, party_id)
    await post_party_rating_prompt(ctx, party_id)


class UndoEndView(discord.ui.View):
    def __init__(self, ctx, party_id, user_id):
        super().__init__(timeout=120)
        self.ctx, self.party_id, self.user_id = ctx, party_id, str(user_id)

    async def interaction_check(self, interaction):
        return str(interaction.user.id) == self.user_id

    @discord.ui.button(label="Undo end", style=discord.ButtonStyle.danger)
    async def undo(self, interaction, button):
        self.stop()
        if _is_organizer(self.ctx, interaction.user) and self.ctx.db.undo_end_party(self.party_id, self.ctx.now(),
                                                                                    interaction.user.id):
            await interaction.response.edit_message(content="↩ The party is running again.", view=None)
            await refresh_party_message(self.ctx, self.party_id)
        else:
            await interaction.response.edit_message(content="Nothing to undo.", view=None)


# ---------------------------------------------------------------- edit composition (organizers)

async def open_edit_panel(ctx, interaction, party_id):
    if not _is_organizer(ctx, interaction.user):
        return await _reply(interaction, "Only organizers can edit a party.")
    party = ctx.db.party(party_id)
    if party is None or party["status"] not in ("confirmed", "done"):
        return await _reply(interaction, "That party is not active.")
    view = EditPartyView(ctx, party_id, interaction.user.id)
    await interaction.response.send_message(view.render(), view=view, ephemeral=True)


class EditPartyView(discord.ui.View):
    """Ephemeral panel: pick who goes out, who comes in (standby or anyone), why, then Swap."""

    def __init__(self, ctx, party_id, user_id):
        super().__init__(timeout=900)
        self.ctx, self.party_id, self.user_id = ctx, party_id, str(user_id)
        self.out_id = self.in_id = self.in_name = None
        self.reason_code, self.notice = "no_show", ""
        self.out_select, self.standby_select = OutSelect(), StandbySelect()
        self.add_item(self.out_select)
        self.add_item(self.standby_select)
        self.add_item(AnyoneSelect())
        self.add_item(ReasonSelect())
        for b in (SwapButton(), ExportButton(), CloseButton()):
            self.add_item(b)
        self.sync()

    async def interaction_check(self, interaction):
        return str(interaction.user.id) == self.user_id and _is_organizer(self.ctx, interaction.user)

    def sync(self):
        db = self.ctx.db
        members = {m["discord_id"]: m for m in db.party_members(self.party_id)}
        pool = [p for p in db.standby_pool(db.party(self.party_id)["slot_id"]) if p not in members]
        if self.out_id not in members:
            self.out_id = None
        if self.in_id in members:
            self.in_id = None
        self.out_select.options = [discord.SelectOption(label=f"{name_of(db, d)} · {m['role']}"[:100], value=d,
                                                        default=d == self.out_id) for d, m in members.items()]
        self.standby_select.options = [discord.SelectOption(label=name_of(db, d)[:100], value=d, default=d == self.in_id)
                                       for d in pool[:25]] or [discord.SelectOption(label="Nobody on standby", value="-")]
        self.standby_select.disabled = not pool
        for item in self.children:
            if isinstance(item, ReasonSelect):
                for o in item.options:
                    o.default = o.value == self.reason_code

    def render(self):
        db = self.ctx.db
        party = db.party(self.party_id)
        lines = [f"**Edit party {party['party_index'] + 1}** · {party['slot_key']}"
                 + (" (already ended; edits still count)" if party["ended_utc"] else "")]
        lines += [f"• <@{m['discord_id']}> {m['role']}" for m in db.party_members(self.party_id)]
        lines.append(f"Swap out: {f'<@{self.out_id}>' if self.out_id else 'pick someone'} → "
                     f"in: {f'<@{self.in_id}>' if self.in_id else 'pick from standby or anyone'}")
        lines.append("The swapped-out player is told in the parties channel and can read the reason you give, "
                     "so keep a custom reason polite.")
        if self.notice:
            lines.append(self.notice)
        return "\n".join(lines)[:1990]

    async def redraw(self, interaction, notice=""):
        self.notice = notice
        self.sync()
        await interaction.response.edit_message(content=self.render(), view=self)


class OutSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Swap out", min_values=1, max_values=1, row=0,
                         options=[discord.SelectOption(label="loading", value="0")])

    async def callback(self, interaction):
        self.view.out_id = self.values[0]
        await self.view.redraw(interaction)


class StandbySelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Replace with someone on standby", min_values=1, max_values=1, row=1,
                         options=[discord.SelectOption(label="loading", value="0")])

    async def callback(self, interaction):
        v = self.view
        if self.values[0] != "-":
            v.in_id, v.in_name = self.values[0], name_of(v.ctx.db, self.values[0])
        await v.redraw(interaction)


class AnyoneSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Or pick anyone from the server", min_values=1, max_values=1, row=2)

    async def callback(self, interaction):
        v, user = self.view, self.values[0]
        if getattr(user, "bot", False):
            return await v.redraw(interaction, "⚠ Bots cannot join a party.")
        v.in_id, v.in_name = str(user.id), user.display_name
        await v.redraw(interaction)


class ReasonSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Why is this swap needed?", min_values=1, max_values=1, row=3,
                         options=[discord.SelectOption(label=label, value=code, default=code == "no_show")
                                  for code, label in REASONS.items()])

    async def callback(self, interaction):
        self.view.reason_code = self.values[0]
        await self.view.redraw(interaction)


class SwapButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Swap", style=discord.ButtonStyle.primary, row=4)

    async def callback(self, interaction):
        v = self.view
        if not (v.out_id and v.in_id):
            return await v.redraw(interaction, "⚠ Pick who goes out and who comes in first.")
        await interaction.response.send_modal(SwapModal(v))


class ExportButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Export JSON", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction):
        v = self.view
        await interaction.response.send_message(
            "Edit the members (and add `reasons` for removed players), then run **/party import** with the file. "
            "The solver's original party is included for reference.",
            file=discord.File(io.BytesIO(dumps(party_json(v.ctx, v.party_id))), filename=f"party-{v.party_id}.json"),
            ephemeral=True)


class CloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Close", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction):
        self.view.stop()
        await interaction.response.edit_message(content="Closed.", view=None)


class SwapModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Swap players")
        self.owner = view
        self.role = discord.ui.TextInput(label="Role (blank = same as swapped-out)", required=False, max_length=6,
                                         placeholder="tank, healer or dps")
        self.text = discord.ui.TextInput(label="Reason (the swapped player can read this)", required=False,
                                         style=discord.TextStyle.paragraph, max_length=300,
                                         placeholder="Required for 'Other'. Be kind: they will see it.")
        self.add_item(self.role)
        self.add_item(self.text)

    async def on_submit(self, interaction):
        v, ctx = self.owner, self.owner.ctx
        party = ctx.db.party(v.party_id)
        members = {m["discord_id"]: m["role"] for m in ctx.db.party_members(v.party_id)}
        if v.out_id not in members or v.in_id in members:
            return await v.redraw(interaction, "⚠ The party changed meanwhile. Pick again.")
        role = (self.role.value or "").strip().lower() or members[v.out_id]
        if role not in ROLES:
            return await v.redraw(interaction, "⚠ The role must be tank, healer or dps. Nothing was changed.")
        text = (self.text.value or "").strip()
        if v.reason_code == "other" and not text:
            return await v.redraw(interaction, "⚠ 'Other' needs a written reason. Nothing was changed.")
        params = dict(out_id=v.out_id, in_id=v.in_id, in_role=role, reason_code=v.reason_code, reason_text=text,
                      in_name=v.in_name)
        warnings = swap_warnings(ctx, party, v.in_id, role) + comp_warnings(ctx, party, {v.out_id: None, v.in_id: role})
        if warnings:
            view = ForceSwapView(ctx, v.party_id, v.user_id, params, warnings)
            await interaction.response.send_message(
                "⚠ **Warnings** for swapping <@%s> in:\n%s\nYou can still swap them in." % (v.in_id, "\n".join("• " + w for w in warnings)),
                view=view, ephemeral=True)
            return
        summary = await run_swap(ctx, interaction, v.party_id, params, [])
        v.out_id = v.in_id = None
        await v.redraw(interaction, summary)


async def run_swap(ctx, interaction, party_id, params, warnings):
    try:
        _, problems = await apply_swap(ctx, interaction.guild_id, party_id, actor_id=interaction.user.id,
                                       warnings=warnings or None, **params)
    except ValueError:
        return "⚠ The party changed meanwhile, so nothing was swapped."
    out = f"✅ Swapped <@{params['out_id']}> out for <@{params['in_id']}>."
    return out + ("\n" + "\n".join("⚠ " + p for p in problems) if problems else "")


class ForceSwapView(discord.ui.View):
    def __init__(self, ctx, party_id, user_id, params, warnings):
        super().__init__(timeout=300)
        self.ctx, self.party_id, self.user_id, self.params, self.warnings = ctx, party_id, str(user_id), params, warnings

    async def interaction_check(self, interaction):
        return str(interaction.user.id) == self.user_id

    @discord.ui.button(label="Swap anyway", style=discord.ButtonStyle.danger)
    async def force(self, interaction, button):
        self.stop()
        summary = await run_swap(self.ctx, interaction, self.party_id, self.params, self.warnings)
        await interaction.response.edit_message(content=summary, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing changed.", view=None)
