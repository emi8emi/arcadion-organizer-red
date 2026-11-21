"""The user panel: one pinned message per server with buttons for what players do most.

The channel is set with /settings set user_panel_channel; the bot posts the message, pins it and remembers its id
(user_panel_message_id) so it can replace or delete its own message later. The buttons are DynamicItems with fixed
custom ids, so they keep working after a restart. Each press runs the same function as the slash command, so the
two can never drift apart. Logic is in plain async functions that take `ctx`, like the other views.
"""
import logging

import discord

from . import character_handlers as chh
from . import characters as ch
from . import handlers

log = logging.getLogger("raidbot")

PANEL_TEXT = ("**Raid signup panel**\n"
              "Use the buttons below. Everything you press is private to you.\n"
              "- **Availability**: pick your days, times and roles for the open run.\n"
              "- **Add character**: start adding a character; you get a code to put in your Lodestone profile.\n"
              "- **Verify character**: after saving the code, press this to finish adding it.\n"
              "- **Remove character** and **My characters**: manage your list.")

# action -> (label, style); the order is the button order
BUTTONS = {"avail": ("Availability", discord.ButtonStyle.primary),
           "add": ("Add character", discord.ButtonStyle.secondary),
           "verify": ("Verify character", discord.ButtonStyle.secondary),
           "remove": ("Remove character", discord.ButtonStyle.secondary),
           "list": ("My characters", discord.ButtonStyle.secondary)}


class PanelButton(discord.ui.DynamicItem[discord.ui.Button], template=r"panel:(?P<action>avail|add|verify|remove|list)"):
    def __init__(self, action):
        label, style = BUTTONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"panel:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["action"])

    async def callback(self, interaction):
        await press(interaction.client.ctx, interaction, self.action)


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for action in BUTTONS:
            self.add_item(PanelButton(action))


class AddCharacterModal(discord.ui.Modal):
    def __init__(self, ctx):
        super().__init__(title="Add a character")
        self.ctx = ctx
        self.name = discord.ui.TextInput(label="Character name", placeholder="Y'shtola Rhul", max_length=40)
        self.world = discord.ui.TextInput(label="World", placeholder="Exodus", max_length=20)
        self.add_item(self.name)
        self.add_item(self.world)

    async def on_submit(self, interaction):
        await chh.character_add(self.ctx, interaction, self.name.value, self.world.value)


class RemoveCharacterView(discord.ui.View):
    """Ephemeral: the player's own characters in a select; choosing one removes it."""

    def __init__(self, ctx, rows):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.select = discord.ui.Select(placeholder="Character to remove",
                                        options=[discord.SelectOption(label=label[:100], value=str(cid))
                                                 for label, cid in rows[:25]])
        self.select.callback = self.chosen
        self.add_item(self.select)

    async def chosen(self, interaction):
        await interaction.response.edit_message(content="Removing...", view=None)
        await chh.character_remove(self.ctx, interaction, int(self.select.values[0]))


async def press(ctx, interaction, action):
    """What each panel button does."""
    if action == "avail":
        return await handlers.availability(ctx, interaction)
    if action == "add":
        return await interaction.response.send_modal(AddCharacterModal(ctx))
    if action == "verify":
        return await chh.character_verify(ctx, interaction)
    if action == "list":
        return await chh.character_list(ctx, interaction)
    if action == "remove":
        rows = chh.character_choices(ctx, interaction.user.id)
        if not rows:
            return await handlers.say(interaction, "You have no verified characters to remove.")
        return await interaction.response.send_message("Which character should I remove?",
                                                       view=RemoveCharacterView(ctx, rows), ephemeral=True)
    await handlers.say(interaction, "That button is not in use any more.")


async def _old_message(ctx, guild_id):
    """The panel message we posted earlier, or None (never posted, or deleted by hand)."""
    chan_id = ctx.db.guild_setting(guild_id, "user_panel_channel_id")
    msg_id = ctx.db.guild_setting(guild_id, "user_panel_message_id")
    if not chan_id or not msg_id:
        return None
    try:
        channel = await ctx.get_channel(int(chan_id))
        return await channel.fetch_message(int(msg_id))
    except Exception:                                  # channel or message gone, or no access
        return None


async def remove_panel(ctx, guild_id):
    """Delete our panel message (best effort) and forget its id. The channel setting is left to the caller."""
    old = await _old_message(ctx, guild_id)
    if old is not None:
        try:
            await old.delete()
        except Exception:
            log.warning("could not delete the old user panel in guild %s", guild_id, exc_info=True)
    ctx.db.set_guild_setting(guild_id, "user_panel_message_id", None)


async def install_panel(ctx, guild_id, channel):
    """Make `channel` the panel channel: replace any earlier panel, post a new one and pin it.
    Returns warning lines for what did not work (the setting is saved either way)."""
    await remove_panel(ctx, guild_id)
    ctx.db.set_guild_setting(guild_id, "user_panel_channel_id", channel.id)
    try:
        message = await channel.send(PANEL_TEXT, view=PanelView())
    except discord.HTTPException:
        return [f"⚠ I could not post the panel in {channel.mention}. I need View Channel and Send Messages there; "
                "run /settings set user_panel_channel again once that is fixed."]
    ctx.db.set_guild_setting(guild_id, "user_panel_message_id", message.id)
    try:
        await message.pin()
    except discord.HTTPException:
        return [f"⚠ I posted the panel in {channel.mention} but could not pin it. Give me Pin Messages "
                "(or Manage Messages) there and pin it by hand, or set the channel again."]
    return []
