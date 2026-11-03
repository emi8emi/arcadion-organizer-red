"""The Confirm / Can't make it buttons under a confirmation message, and the decline flow.

DynamicItems: the slot id is in the custom_id, so the buttons keep working after a restart. The logic is in plain
async functions that take `ctx`, so tests can call them with fake interactions.
"""
import logging

import discord

from . import confirmations as cf
from .db import parse_iso

log = logging.getLogger("raidbot")


async def _reply(interaction, text, **kwargs):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(text, ephemeral=True, **kwargs)


class ConfirmButton(discord.ui.DynamicItem[discord.ui.Button], template=r"cfm:(?P<slot>[0-9]+)"):
    def __init__(self, slot_id):
        super().__init__(discord.ui.Button(label="I'll be there", style=discord.ButtonStyle.success,
                                           custom_id=f"cfm:{slot_id}"))
        self.slot_id = int(slot_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["slot"]))

    async def callback(self, interaction):
        await press_confirm(interaction.client.ctx, interaction, self.slot_id)


class CancelButton(discord.ui.DynamicItem[discord.ui.Button], template=r"cnc:(?P<slot>[0-9]+)"):
    def __init__(self, slot_id):
        super().__init__(discord.ui.Button(label="Can't make it", style=discord.ButtonStyle.danger,
                                           custom_id=f"cnc:{slot_id}"))
        self.slot_id = int(slot_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["slot"]))

    async def callback(self, interaction):
        await press_cancel(interaction.client.ctx, interaction, self.slot_id)


class ConfirmView(discord.ui.View):
    def __init__(self, slot_id):
        super().__init__(timeout=None)
        self.add_item(ConfirmButton(slot_id))
        self.add_item(CancelButton(slot_id))


def _check(ctx, interaction, slot_id):
    """(slot, run, error text): the presser must be seated in this slot and the slot must not be over."""
    slot = cf.get_slot(ctx.db, slot_id)
    if slot is None:
        return None, None, "That slot no longer exists."
    run = ctx.db.run(slot["run_id"])
    if str(interaction.user.id) not in cf.member_ids(ctx.db, slot_id):
        return slot, run, "You are not in a party for this slot."
    if cf.is_over(slot, ctx.now()):
        return slot, run, "This slot is already over."
    return slot, run, None


async def press_confirm(ctx, interaction, slot_id):
    slot, run, error = _check(ctx, interaction, slot_id)
    if error:
        return await _reply(interaction, error)
    cf.answer(ctx.db, run["id"], slot_id, str(interaction.user.id), "confirmed", ctx.now())
    when = discord.utils.format_dt(parse_iso(slot["start_utc"]), "F")
    await _reply(interaction, f"Confirmed for {when}. See you there!")
    await cf.refresh_prompts(ctx, slot_id)


async def press_cancel(ctx, interaction, slot_id):
    slot, run, error = _check(ctx, interaction, slot_id)
    if error:
        return await _reply(interaction, error)
    await _reply(interaction, "Why can't you make it? The default is that you are unavailable at this slot, which "
                              "takes the slot out of your signup so the organizers can refill the party.",
                 view=DeclineView(ctx, slot_id, str(interaction.user.id)))


class ReasonSelect(discord.ui.Select):
    def __init__(self, current):
        super().__init__(placeholder="Reason", row=0, options=[
            discord.SelectOption(label=label, value=code, default=(code == current))
            for code, label in cf.CANCEL_REASONS.items()])

    async def callback(self, interaction):
        view = self.view
        view.code = self.values[0]
        for opt in self.options:
            opt.default = opt.value == view.code
        await interaction.response.edit_message(view=view)


class DeclineView(discord.ui.View):
    """Ephemeral: pick a reason (default: unavailable at this slot) and send it."""

    def __init__(self, ctx, slot_id, user_id):
        super().__init__(timeout=300)
        self.ctx, self.slot_id, self.user_id = ctx, slot_id, user_id
        self.code = cf.DEFAULT_CANCEL
        self.add_item(ReasonSelect(self.code))

    @discord.ui.button(label="Send", style=discord.ButtonStyle.danger, row=1)
    async def send(self, interaction, button):
        if self.code == "other":
            return await interaction.response.send_modal(OtherReasonModal(self.ctx, self.slot_id))
        await submit_decline(self.ctx, interaction, self.slot_id, self.code, None)

    @discord.ui.button(label="Never mind", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await interaction.response.edit_message(content="Nothing changed.", view=None)


class OtherReasonModal(discord.ui.Modal, title="Why can't you make it?"):
    text = discord.ui.TextInput(label="Reason", max_length=200, required=True)

    def __init__(self, ctx, slot_id):
        super().__init__()
        self.ctx, self.slot_id = ctx, slot_id

    async def on_submit(self, interaction):
        await submit_decline(self.ctx, interaction, self.slot_id, "other", str(self.text))


async def submit_decline(ctx, interaction, slot_id, code, text):
    """Record the decline, change the signup, update the messages and tell the organizers."""
    slot, run, error = _check(ctx, interaction, slot_id)
    if error:
        return await _reply(interaction, error)
    if code == "other" and not (text or "").strip():
        return await _reply(interaction, "Please write a reason.")
    me = str(interaction.user.id)
    effect = cf.apply_decline(ctx.db, run, slot, me, code, text, ctx.now())
    approved = any(p["status"] != "draft" for p, _ in cf.slot_parties(ctx.db, slot_id))
    await _reply(interaction, f"Noted: {cf.cancel_label(code, text)}. {effect} Thank you for telling us.")
    await cf.refresh_prompts(ctx, slot_id)
    await cf.notify_decline(ctx, getattr(interaction, "channel", None), run, slot, me, code, text, approved)
