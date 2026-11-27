"""/helper: one private panel for a fight's helpers, the helper role and players who need support.

The functions at the top do the work and return the text to show; the panel below calls them. A helper is someone
granted by an organizer here or a holder of the fight's helper role (see `handlers.role_helper_ids`). A player who
needs support is seated with that many helpers or higher-prog players (see the README and the solver's needs_support).
"""
import discord

from . import handlers

MAX_NEED = 4


# ---------------------------------------------------------------- the actions (text out)

def grant(ctx, fight, members, by):
    for m in members:
        ctx.db.grant_helper(fight["id"], m.id, by, ctx.now())
    return "Helpers added: " + ", ".join(f"<@{m.id}>" for m in members) + ". Export again to send it to the solver."


def revoke(ctx, fight, did):
    return (f"<@{did}> is no longer a granted helper." if ctx.db.revoke_helper(fight["id"], did)
            else f"<@{did}> was not a granted helper.")


def flag(ctx, fight, member, helpers, required, by):
    if not 1 <= helpers <= 7:
        return "Helpers must be between 1 and 7."
    ctx.db.set_support_need(fight["id"], member.id, helpers, required, by, ctx.now())
    cap = ctx.cfg.get("max_helpers_per_party")
    note = (f" The solver caps helpers per party at {cap}, so the rest has to come from players with a higher prog."
            if cap is not None and helpers > cap else "")
    return (f"<@{member.id}> will be seated with {helpers} helper(s) or higher-prog player(s) "
            f"({'required: left out if it cannot be met' if required else 'preferred: the solver tries its best'})."
            + note + " Export again to send it to the solver.")


def unflag(ctx, fight, did):
    return (f"<@{did}> no longer needs support." if ctx.db.clear_support_need(fight["id"], did)
            else f"<@{did}> had no support need.")


def set_role(ctx, fight, guild_id, role, by):
    ctx.db.set_helper_role(fight["id"], guild_id, role.id, by, ctx.now())
    return f"Everyone with {role.mention} now counts as a helper for {fight['name']}."


def clear_role(ctx, fight, guild_id):
    return ("The helper role is cleared." if ctx.db.clear_helper_role(fight["id"], guild_id)
            else "No helper role was set.")


def render(ctx, fight, guild_id, notice=""):
    db = ctx.db
    role_id, granted, needs = db.helper_role(fight["id"], guild_id), sorted(db.helper_ids(fight["id"])), \
        db.support_needs(fight["id"])
    lines = [f"**Helpers · {fight['name']}**"]
    if role_id is None:
        lines.append("Helper role: not set")
    else:
        members = ctx.role_members(guild_id, role_id) if ctx.role_members else None
        lines.append(f"Helper role: <@&{role_id}> · " + (f"{len(members)} member(s)" if members is not None else
                     "⚠ the bot cannot read role members; turn on the Server Members Intent (MEMBERS_INTENT=1)"))
    lines.append("Granted helpers: " + (", ".join(f"<@{d}>" for d in granted) or "nobody"))
    lines.append("Needs support: " + (", ".join(f"<@{d}> ×{n['helpers']}" + (" (required)" if n["required"] else "")
                                               for d, n in sorted(needs.items())) or "nobody"))
    if notice:
        lines.append("\n" + notice)
    lines.append("\nA helper, or anyone with a higher prog, counts as company for a player who needs support.")
    return "\n".join(lines)[:1990]


# ---------------------------------------------------------------- the panel

class HelperView(discord.ui.View):
    def __init__(self, ctx, fight_id, user_id, guild_id):
        super().__init__(timeout=900)
        self.ctx, self.fight_id, self.user_id, self.guild_id, self.notice = ctx, fight_id, user_id, guild_id, ""
        fights = ctx.db.fights()
        if len(fights) > 1:
            self.fight_select = discord.ui.Select(
                placeholder="Fight", row=0, min_values=1, max_values=1,
                options=[discord.SelectOption(label=f["name"][:100], value=str(f["id"])) for f in fights[:25]])
            self.fight_select.callback = self.fight_picked
            self.add_item(self.fight_select)
        else:
            self.fight_select = None
        self.add_users = discord.ui.UserSelect(placeholder="Make these players helpers", row=1, min_values=1, max_values=10)
        self.add_users.callback = self.users_picked
        self.role_select = discord.ui.RoleSelect(placeholder="Helper role: everyone with it is a helper", row=2,
                                                 min_values=1, max_values=1)
        self.role_select.callback = self.role_picked
        self.remove = discord.ui.Select(placeholder="Remove a helper or a support need", row=3, min_values=1,
                                        max_values=1, options=[discord.SelectOption(label="loading", value="-")])
        self.remove.callback = self.remove_picked
        self.flag_button = discord.ui.Button(label="Needs support…", style=discord.ButtonStyle.primary, row=4)
        self.flag_button.callback = self.open_flag
        self.clear_role_button = discord.ui.Button(label="Clear helper role", style=discord.ButtonStyle.secondary, row=4)
        self.clear_role_button.callback = self.clear_the_role
        for item in (self.add_users, self.role_select, self.remove, self.flag_button, self.clear_role_button):
            self.add_item(item)
        self.sync()

    @property
    def fight(self):
        return self.ctx.db.fight(self.fight_id)

    def sync(self):
        db, fid = self.ctx.db, self.fight_id
        if self.fight_select:
            for o in self.fight_select.options:
                o.default = o.value == str(fid)
        options = [discord.SelectOption(label=f"{name_of(self.ctx, d)} · granted helper"[:100], value=f"h:{d}")
                   for d in sorted(db.helper_ids(fid))]
        options += [discord.SelectOption(label=f"{name_of(self.ctx, d)} · needs {n['helpers']}"[:100], value=f"s:{d}")
                    for d, n in sorted(db.support_needs(fid).items())]
        self.remove.options = options[:25] or [discord.SelectOption(label="Nothing to remove", value="-")]
        self.remove.disabled = not options
        self.clear_role_button.disabled = db.helper_role(fid, self.guild_id) is None

    def text(self):
        return render(self.ctx, self.fight, self.guild_id, self.notice)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This panel is not yours. Use /helper.", ephemeral=True)
            return False
        return True

    async def redraw(self, interaction, notice=""):
        self.notice = notice
        self.sync()
        await interaction.response.edit_message(content=self.text(), view=self)

    async def fight_picked(self, interaction):
        self.fight_id = int(self.fight_select.values[0])
        await self.redraw(interaction)

    async def users_picked(self, interaction):
        people = [m for m in self.add_users.values if not getattr(m, "bot", False)]
        await self.redraw(interaction, grant(self.ctx, self.fight, people, interaction.user.id) if people
                          else "Bots cannot be helpers.")

    async def role_picked(self, interaction):
        await self.redraw(interaction, set_role(self.ctx, self.fight, self.guild_id, self.role_select.values[0],
                                                interaction.user.id))

    async def clear_the_role(self, interaction):
        await self.redraw(interaction, clear_role(self.ctx, self.fight, self.guild_id))

    async def remove_picked(self, interaction):
        kind, _, did = self.remove.values[0].partition(":")
        if kind == "h":
            note = revoke(self.ctx, self.fight, did)
        elif kind == "s":
            note = unflag(self.ctx, self.fight, did)
        else:
            note = ""
        await self.redraw(interaction, note)

    async def open_flag(self, interaction):
        await interaction.response.send_modal(SupportModal(self))


def name_of(ctx, did):
    row = ctx.db.conn.execute("SELECT display_name FROM players WHERE discord_id = ?", (str(did),)).fetchone()
    return (row["display_name"] if row and row["display_name"] else f"player {did}")


class SupportModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Needs support")
        self.owner = view
        self.who = discord.ui.UserSelect(min_values=1, max_values=1)
        self.count = discord.ui.Select(min_values=1, max_values=1, options=[
            discord.SelectOption(label=f"{n} helper{'s' if n > 1 else ''}", value=str(n), default=n == 1)
            for n in range(1, MAX_NEED + 1)])
        self.mode = discord.ui.Select(min_values=1, max_values=1, options=[
            discord.SelectOption(label="Preferred", value="soft", default=True,
                                 description="The solver tries its best and still seats them without."),
            discord.SelectOption(label="Required", value="hard",
                                 description="Leave them out if the helpers cannot be found.")])
        self.add_item(discord.ui.Label(text="Player", component=self.who))
        self.add_item(discord.ui.Label(text="Seat them with", component=self.count,
                                       description="Helpers or players with a higher prog."))
        self.add_item(discord.ui.Label(text="How strict", component=self.mode))

    async def on_submit(self, interaction):
        v = self.owner
        member = self.who.values[0]
        if getattr(member, "bot", False):
            return await v.redraw(interaction, "Bots do not play.")
        note = flag(v.ctx, v.fight, member, int(self.count.values[0]), self.mode.values[0] == "hard", interaction.user.id)
        await v.redraw(interaction, note)


# ---------------------------------------------------------------- /helper

async def start(ctx, interaction, content=None):
    if not await handlers.require_organizer(ctx, interaction):
        return
    fights = ctx.db.fights()
    fight = ctx.db.find_fight(content) if content else (fights[0] if fights else None)
    if fight is None:
        return await handlers.say(interaction, f"No fight called '{content}'." if content else "No fights are set up yet.")
    view = HelperView(ctx, fight["id"], interaction.user.id, interaction.guild_id)
    await interaction.response.send_message(view.text(), view=view, ephemeral=True)
