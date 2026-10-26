"""Slash command registration. Thin wrappers: the real logic is in handlers.py."""
from typing import Optional

import discord
from discord import app_commands
from zoneinfo import available_timezones

from . import handlers



# 1. Cache the sorted list of timezones globally at startup 
# This avoids scanning 600 timezones on every single keystroke
ALL_TIMEZONES = sorted(list(available_timezones()))

# 2. Define the autocomplete callback function
async def timezone_autocomplete(
    interaction: discord.Interaction, 
    current: str
) -> list[app_commands.Choice[str]]:
    """Filters timezones on the fly based on user input (max 25 results)."""
    cleaned_input = current.strip().lower()
    
    # Filter the cached list to find partial matches
    matches = [
        tz for tz in ALL_TIMEZONES 
        if cleaned_input in tz.lower()
    ]
    
    # Discord cuts off autocomplete arrays at exactly 25 items
    return [
        app_commands.Choice(name=tz, value=tz)
        for tz in matches[:25]
    ]



class RunGroup(app_commands.Group):
    def __init__(self, ctx):
        super().__init__(name="run", description="Organizers: manage signup runs", guild_only=True)
        self.ctx = ctx

    @app_commands.command(name="create", description="Create a run (draft) with its slot grid")
    @app_commands.autocomplete(timezone=timezone_autocomplete)
    @app_commands.describe(
        name="Name of the run, e.g. UWU week 1",
        content="The fight, e.g. Ultima Weapon (Ultimate)",
        start_date="First day, like 2026-11-02",
        end_date="Last day (inclusive), like 2026-11-08",
        signup_deadline="Eastern time, like 2026-10-30 18:00",
        earliest_start="Optional: first slot start to offer, like 17:00",
        latest_start="Optional: last slot start to offer, like 20:00",
        timezone="Optional: Iana timezone (e.g., America/New_York)")
    async def create(self, interaction: discord.Interaction, name: str, content: str, start_date: str,
                     end_date: str, signup_deadline: str, earliest_start: Optional[str] = None,
                     latest_start: Optional[str] = None, timezone: Optional[str] = None):
        await handlers.run_create(self.ctx, interaction, name, content, start_date, end_date,
                                  signup_deadline, earliest_start, latest_start, timezone)

    @create.autocomplete("content")
    async def create_content_choices(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=f["name"], value=f["name"]) for f in self.ctx.db.fights()
                if current.lower() in f["name"].lower()][:25]

    @app_commands.command(name="open", description="Open a run for signups")
    @app_commands.describe(name="Run name (default: the newest run)")
    async def open(self, interaction: discord.Interaction, name: Optional[str] = None):
        await handlers.run_open(self.ctx, interaction, name)

    @app_commands.command(name="close", description="Close signups for a run")
    @app_commands.describe(name="Run name (default: the newest run)")
    async def close(self, interaction: discord.Interaction, name: Optional[str] = None):
        await handlers.run_close(self.ctx, interaction, name)

    @app_commands.command(name="status", description="Live signup heatmap: ticks per slot, split by role")
    @app_commands.describe(name="Run name (default: the newest run)", day="Optional: only this day, like 2026-11-02")
    async def status(self, interaction: discord.Interaction, name: Optional[str] = None,
                     day: Optional[str] = None):
        await handlers.run_status(self.ctx, interaction, name, day)


class DraftGroup(app_commands.Group):
    def __init__(self, ctx):
        super().__init__(name="draft", description="Organizers: solver hand-off", guild_only=True)
        self.ctx = ctx

    @app_commands.command(name="export", description="Export signups as solver input (JSON file)")
    @app_commands.describe(name="Run name (default: the newest run)")
    async def export(self, interaction: discord.Interaction, name: Optional[str] = None):
        await handlers.draft_export(self.ctx, interaction, name)

    @app_commands.command(name="import", description="Import a solver draft (refused if signups changed)")
    @app_commands.describe(draft_file="The draft.json the solver wrote", name="Run name (default: the newest run)")
    async def import_(self, interaction: discord.Interaction, draft_file: discord.Attachment,
                      name: Optional[str] = None):
        await handlers.draft_import(self.ctx, interaction, draft_file, name)


class HelperGroup(app_commands.Group):
    def __init__(self, ctx):
        super().__init__(name="helper", description="Organizers: helper status", guild_only=True)
        self.ctx = ctx

    @app_commands.command(name="grant", description="Mark someone as a helper for a fight")
    @app_commands.describe(member="Who", content="The fight (optional while there is only one)")
    async def grant(self, interaction: discord.Interaction, member: discord.Member,
                    content: Optional[str] = None):
        await handlers.helper_grant(self.ctx, interaction, member, content)

    @app_commands.command(name="revoke", description="Remove helper status for a fight")
    @app_commands.describe(member="Who", content="The fight (optional while there is only one)")
    async def revoke(self, interaction: discord.Interaction, member: discord.Member,
                     content: Optional[str] = None):
        await handlers.helper_revoke(self.ctx, interaction, member, content)


def register(tree, ctx):
    tree.add_command(RunGroup(ctx))
    tree.add_command(DraftGroup(ctx))
    tree.add_command(HelperGroup(ctx))

    @tree.command(name="availability", description="Sign up: your roles, prog and the slots you can play")
    # no autocomplete for this, since i'm not sure if it's spammable
    @app_commands.describe(timezone="Optional, like America/Chicago (slot times already show in your local time)")
    @app_commands.guild_only()
    async def availability(interaction: discord.Interaction, timezone: Optional[str] = None):
        await handlers.availability(ctx, interaction, timezone)

    @tree.command(name="limits", description="See or change your limits for the open run")
    @app_commands.describe(max_per_day="Most parties you want per day", max_per_run="Most parties for the whole run",
                           drop_checkpoints="Checkpoints below your prog you accept (0 = not OK)")
    @app_commands.guild_only()
    async def limits(interaction: discord.Interaction, max_per_day: Optional[int] = None,
                     max_per_run: Optional[int] = None, drop_checkpoints: Optional[int] = None):
        await handlers.limits(ctx, interaction, max_per_day, max_per_run, drop_checkpoints)

    @tree.command(name="myschedule", description="Your signup and confirmed parties")
    @app_commands.guild_only()
    async def myschedule(interaction: discord.Interaction):
        await handlers.myschedule(ctx, interaction)
