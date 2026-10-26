"""The Discord client: builds the command tree and syncs it on startup."""
import logging

import discord
from discord import app_commands

from .commands import register
from .config import ROOT
from .db import DB
from .grid import GridConfig
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

    async def on_ready(self):
        log.info("Logged in as %s", self.user)

    async def on_tree_error(self, interaction, error):
        log.exception("command failed", exc_info=error)
        msg = "Something went wrong. The organizers can check the bot log."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
