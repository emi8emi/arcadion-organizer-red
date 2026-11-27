"""Buttons on a party's message in its thread: "Party ended" (any member, organizers can override or undo),
"Edit composition" (organizers), and the "Why was I swapped?" button on the swap notice.

The buttons are DynamicItems: the party (or change) id is in the custom_id, so they work after a restart.
The logic lives in plain async functions that take `ctx`, so tests can call them with fake interactions.
"""
import logging

import discord

from .db import parse_iso
from .parties import minutes_played, post_party_rating_prompt, reason_label, refresh_party_message

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
    if change is None or change["kind"] not in ("swap", "remove") or not (
            me == change["player_id"] or _is_organizer(ctx, interaction.user)):
        return await _reply(interaction, "This notice is not about you.")
    verb = "swapped out" if change["kind"] == "swap" else "taken out of the party"
    await _reply(interaction, f"You were {verb} by <@{change['actor_id']}>.\n"
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
    """"Edit composition": the slot editor (see slot_editor.py)."""
    from .slot_editor import open_edit_panel as open_slot_editor
    await open_slot_editor(ctx, interaction, party_id)
