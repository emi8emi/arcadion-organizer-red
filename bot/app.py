"""The Discord client: builds the command tree and syncs it on startup."""
import logging

import discord
from discord import app_commands
from discord.ext import tasks

from .commands import register
from .config import ROOT
from .db import DB
from .grid import GridConfig
from .posting import post_rating_prompts
from .reminders import send_due_reminders
from .snapshots import take_due_snapshots
from .views import RatePartyButton
from .parties import auto_end_parties
from .party_views import PartyEditButton, PartyEndedButton, WhySwappedButton
from .services import Ctx

log = logging.getLogger("raidbot")


def build_ctx(cfg):
    path = cfg["db_path"]
    path = path if path == ":memory:" or path.startswith("/") else str(ROOT / path)
    return Ctx(db=DB(path), cfg=cfg, grid=GridConfig.from_config(cfg))


class RaidBot(discord.Client):
    def __init__(self, ctx, guild_id=None):
        super().__init__(intents=discord.Intents.default())      # slash commands need no privileged intents
        self.ctx, self.guild_id = ctx, guild_id
        ctx.get_channel = self._get_channel
        self.tree = app_commands.CommandTree(self)
        register(self.tree, ctx)
        self.tree.on_error = self.on_tree_error

    async def setup_hook(self):
        if self.guild_id:                                         # instant, one server
            guild = discord.Object(id=int(self.guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:                                                     # global, can take up to an hour to appear
            await self.tree.sync()
        if not self.ctx.cfg.get("reminder_channel_id"):
            log.warning("No reminder_channel_id set: the bot will not remind organizers to run /draft export.")
        self.add_dynamic_items(RatePartyButton, PartyEndedButton, PartyEditButton, WhySwappedButton)           # the thread buttons keep working after a restart
        self.reminder_loop.start()

    async def _get_channel(self, channel_id):
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    @tasks.loop(minutes=1)
    async def reminder_loop(self):
        """Once a minute: freeze signups of days that just closed, then nag organizers about days not yet exported.

        No automatic solving."""
        try:
            take_due_snapshots(self.ctx.db, self.ctx.now(), self.ctx.cfg)     # freeze each day's signups at its cutoff
        except Exception:
            log.exception("taking day snapshots failed")
        try:
            await auto_end_parties(self.ctx)                                     # slots that ran out of time
        except Exception:
            log.exception("auto-ending parties failed")
        try:
            await post_rating_prompts(self.ctx)                                  # slots that just ended
        except Exception:
            log.exception("posting rating prompts failed")
        try:
            await send_due_reminders(self.ctx, self._get_channel)
        except Exception:
            log.exception("reminder check failed")       # keep the loop alive

    @reminder_loop.before_loop
    async def _before_reminders(self):
        await self.wait_until_ready()

    async def on_ready(self):
        # sync commands
        # self.tree.clear_commands(guild=None)
        # await self.tree.sync()
        log.info("Logged in as %s", self.user)

    async def on_tree_error(self, interaction, error):
        log.exception("command failed", exc_info=error)
        msg = "Something went wrong. The organizers can check the bot log."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
