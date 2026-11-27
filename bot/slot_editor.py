"""The organizer's slot editor: change the parties of one slot, stage the changes, apply them together.

Opened by "Edit composition" under a party message. It shows every approved party of the slot and the standby pool.
The organizer picks an action, who it is about and what to do (Stage queues it as one operation, see party_edit.py),
looks at the preview and the warnings, and presses Apply: one write, one message per affected player.
"""
import io
import logging
from types import SimpleNamespace

import discord

from . import party_edit
from .config import ROLES, role_label
from .parties import REASONS, dumps, name_of, party_json
from .services import checkpoint_label

log = logging.getLogger("raidbot")

ACTIONS = {
    "swap": "Swap a player for someone else",
    "move": "Move a player to another party",
    "trade": "Trade two players between parties",
    "remove": "Take a player out (leaves the party short)",
    "add": "Add a player to a short party",
    "role": "Change a player's role",
    "pf_add": "Fill a seat from Party Finder",
    "pf_remove": "Free a Party Finder seat",
}
NEEDS_REASON = ("swap", "remove")
ANYONE = "?"


def _is_organizer(ctx, user):
    from .handlers import is_organizer
    return is_organizer(ctx, user)


class SlotEditView(discord.ui.View):
    def __init__(self, ctx, slot_id, user_id, guild_id):
        super().__init__(timeout=900)
        self.ctx, self.slot_id, self.user_id, self.guild_id = ctx, slot_id, str(user_id), guild_id
        self.ops, self.notice = [], ""
        self.action, self.subject, self.object = "swap", None, None
        self.reason_code = "no_show"
        self.anyone = None                                  # (discord_id, display name) picked with "Someone else"
        self.action_select = ActionSelect()
        self.subject_select = SubjectSelect()
        self.object_select = ObjectSelect()
        self.reason_select = ReasonSelect()
        for item in (self.action_select, self.subject_select, self.object_select, self.reason_select,
                     StageButton(), UndoButton(), ApplyButton(), ExportButton(), CloseButton()):
            self.add_item(item)
        self.refresh()

    # ------------------------------------------------------------ state

    def preview(self):
        return party_edit.plan(self.ctx, self.slot_id, self.ops)

    def members(self, state):
        """[(party_id, discord_id, role)] of a plan state, in party order."""
        return [(pid, did, m["role"]) for pid, st in sorted(state.items(), key=lambda kv: kv[1]["index"])
                for did, m in st["members"].items()]

    def standby(self):
        db = self.ctx.db
        taken = {did for st in party_edit.load(self.ctx, self.slot_id).values() for did in st["members"]}
        return [d for d in db.standby_pool(self.slot_id) if d not in taken]

    def refresh(self):
        """Rebuild every dropdown for the current action and the staged edits."""
        ctx, db = self.ctx, self.ctx.db
        plan = self.preview()
        state = plan.after
        label = lambda pid: f"Party {state[pid]['index'] + 1}"                                   # noqa: E731
        name = lambda d: name_of(db, d)[:40]                                                      # noqa: E731
        a = self.action
        for o in self.action_select.options:
            o.default = o.value == a
        who = [discord.SelectOption(label=f"{label(p)} · {name(d)} · {role_label(r)}"[:100], value=d,
                                    default=d == self.subject) for p, d, r in self.members(state)][:25]
        if a in ("add", "pf_add"):
            who = [discord.SelectOption(label=f"{label(pid)} · short by {party_edit.short_by(ctx, st)}"[:100],
                                        value=str(pid), default=str(pid) == self.subject)
                   for pid, st in sorted(state.items(), key=lambda kv: kv[1]["index"])
                   if party_edit.short_by(ctx, st) > 0][:25]
        elif a == "pf_remove":
            who = [discord.SelectOption(label=f"{label(pid)} · {role_label(x['role'])}"
                                        + (f" · {x['name']}" if x.get("name") else ""), value=f"{pid}:{i}",
                                        default=f"{pid}:{i}" == self.subject)
                   for pid, st in state.items() for i, x in enumerate(st["pf"])][:25]
        self._set(self.subject_select, who, {
            "add": "Which party needs someone?", "pf_add": "Which party needs a seat filled?",
            "pf_remove": "Which Party Finder seat?"}.get(a, "Who is this about?"))
        obj, hint, off = [], "Nothing more to pick", False
        if a in ("swap", "add"):
            pool = self.standby()[:23]
            obj = [discord.SelectOption(label=f"{name(d)} (standby)", value=d, default=d == self.object) for d in pool]
            if self.anyone:
                obj.append(discord.SelectOption(label=f"{self.anyone[1][:90]} (picked)", value=self.anyone[0],
                                                default=self.object == self.anyone[0]))
            obj.append(discord.SelectOption(label="Someone else from the server…", value=ANYONE))
            hint = "Replace with / add whom?" if a == "swap" else "Who joins?"
        elif a == "move":
            obj = [discord.SelectOption(label=f"{label(pid)}"[:100], value=str(pid), default=str(pid) == self.object)
                   for pid in state if not self.subject or pid != party_of(state, self.subject)][:25]
            hint = "Move to which party?"
        elif a == "trade":
            mine = party_of(state, self.subject)
            obj = [discord.SelectOption(label=f"{label(p)} · {name(d)} · {role_label(r)}"[:100], value=d,
                                        default=d == self.object)
                   for p, d, r in self.members(state) if p != mine][:25]
            hint = "Trade with whom?"
        elif a in ("role", "pf_add"):
            obj = [discord.SelectOption(label=role_label(r), value=r, default=r == self.object) for r in ROLES]
            hint = "Which role?"
        else:
            off = True
        self._set(self.object_select, obj, hint, disabled=off)
        for o in self.reason_select.options:
            o.default = o.value == self.reason_code
        self.reason_select.disabled = a not in NEEDS_REASON

    @staticmethod
    def _set(select, options, placeholder, disabled=False):
        select.placeholder = placeholder
        select.options = options or [discord.SelectOption(label="Nothing to pick", value="-")]
        select.disabled = disabled or not options

    # ------------------------------------------------------------ text

    def render(self):
        ctx, db = self.ctx, self.ctx.db
        plan = self.preview()
        slot = db.conn.execute("SELECT * FROM slots WHERE id = ?", (self.slot_id,)).fetchone()
        run = db.run(slot["run_id"])
        cps = db.checkpoints(run["fight_id"])
        lines = [f"**Edit slot** · {slot['slot_key'].replace('T', ' ')}"
                 + (f" · {len(self.ops)} staged change(s), nothing applied yet" if self.ops else "")]
        parties = {p["id"]: p for p in db.active_slot_parties(self.slot_id)}
        for pid, st in sorted(plan.after.items(), key=lambda kv: kv[1]["index"]):
            mark = "✏ " if pid in plan.changed else ""
            ended = " (ended)" if parties[pid]["ended_utc"] else ""
            lines.append(f"**{mark}Party {st['index'] + 1}** · {checkpoint_label(cps, parties[pid]['target_idx'])}{ended}")
            lines += party_edit.party_lines(ctx, st)
        pool = self.standby()
        lines.append("Standby: " + (", ".join(name_of(db, d) for d in pool[:12]) + (f" and {len(pool) - 12} more"
                                                                                       if len(pool) > 12 else "")
                                    if pool else "nobody"))
        if plan.lines:
            lines.append("**Staged:**")
            lines += [f"{n}. {t}" for n, t in enumerate(plan.lines, 1)]
        lines += ["⚠ " + e for e in plan.errors] + ["⚠ " + w for w in plan.warnings]
        if self.notice:
            lines.append(self.notice)
        lines.append("Players taken out are told in the parties channel and can read the reason, so keep it polite.")
        return "\n".join(lines)[:1990]

    async def interaction_check(self, interaction):
        return str(interaction.user.id) == self.user_id and _is_organizer(self.ctx, interaction.user)

    async def redraw(self, interaction, notice=""):
        self.notice = notice
        self.refresh()
        await interaction.response.edit_message(content=self.render(), view=self)

    # ------------------------------------------------------------ staging and applying

    def build_op(self, text="", name=None):
        """The operation for the current picks, or (None, why not)."""
        a, s, o = self.action, self.subject, self.object
        state = self.preview().after
        reason = {"code": self.reason_code, "text": text}
        if a == "swap":
            if not (s and o and o != ANYONE):
                return None, "Pick who goes out and who comes in."
            return {"op": "swap", "party": party_of(state, s), "out": s, "in": o, "reason": reason}, ""
        if a == "remove":
            if not s:
                return None, "Pick who is taken out."
            return {"op": "remove", "party": party_of(state, s), "player": s, "reason": reason}, ""
        if a == "move":
            if not (s and o):
                return None, "Pick the player and the party they move to."
            return {"op": "move", "player": s, "to": int(o)}, ""
        if a == "trade":
            if not (s and o):
                return None, "Pick the two players to trade."
            return {"op": "trade", "a": s, "b": o}, ""
        if a == "role":
            if not (s and o):
                return None, "Pick the player and their new role."
            return {"op": "role", "party": party_of(state, s), "player": s, "role": o}, ""
        if a == "add":
            if not (s and o and o != ANYONE):
                return None, "Pick the short party and who joins."
            role = self.open_role(int(s))
            return {"op": "add", "party": int(s), "player": o, "role": role}, ""
        if a == "pf_add":
            if not (s and o):
                return None, "Pick the party and the role of the Party Finder player."
            return {"op": "pf_add", "party": int(s), "role": o, "name": name}, ""
        if a == "pf_remove":
            if not s:
                return None, "Pick the Party Finder seat."
            pid, idx = s.split(":")
            return {"op": "pf_remove", "party": int(pid), "pf": int(idx)}, ""
        return None, "Pick an action."

    def open_role(self, party_id):
        """The role an added player takes: the first role the party is short of (else the first role)."""
        st = self.preview().after[party_id]
        want = self.ctx.cfg["party"]
        have = {r: sum(x == r for x in party_edit.roles_of(st)) for r in ROLES}
        return next((r for r in ROLES if want.get(r, 0) > have[r]), ROLES[0])

    async def stage(self, interaction, text="", name=None):
        op, why = self.build_op(text, name)
        if op is None:
            return await self.redraw(interaction, "⚠ " + why)
        self.ops.append(op)
        check = self.preview()
        if check.errors:
            self.ops.pop()
            return await self.redraw(interaction, "⚠ Not staged: " + " ".join(check.errors))
        self.subject = self.object = None
        await self.redraw(interaction)

    async def run_apply(self, interaction, warnings=None):
        await interaction.response.defer()          # the edit makes several Discord calls: answer within 3 s first
        result, _, problems = await party_edit.apply(self.ctx, self.guild_id, self.slot_id, self.ops, interaction.user.id,
                                                     warnings=warnings)
        if result.errors:
            self.notice = "⚠ Nothing was applied: " + " ".join(result.errors)
        else:
            self.ops = []
            self.notice = "\n".join(result.lines + ["⚠ " + p for p in problems])
        self.refresh()
        await interaction.edit_original_response(content=self.render(), view=self)


def party_of(state, did):
    for pid, st in state.items():
        if did in st["members"]:
            return pid
    return None


# ---------------------------------------------------------------- controls

class ActionSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="What do you want to do?", min_values=1, max_values=1, row=0,
                         options=[discord.SelectOption(label=text, value=code) for code, text in ACTIONS.items()])

    async def callback(self, interaction):
        v = self.view
        v.action, v.subject, v.object = self.values[0], None, None
        await v.redraw(interaction)


class SubjectSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Who is this about?", min_values=1, max_values=1, row=1,
                         options=[discord.SelectOption(label="loading", value="-")])

    async def callback(self, interaction):
        v = self.view
        v.subject = None if self.values[0] == "-" else self.values[0]
        v.object = None
        await v.redraw(interaction)


class ObjectSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Then pick…", min_values=1, max_values=1, row=2,
                         options=[discord.SelectOption(label="loading", value="-")])

    async def callback(self, interaction):
        v, value = self.view, self.values[0]
        if value == ANYONE:
            return await interaction.response.send_modal(AnyoneModal(v))
        v.object = None if value == "-" else value
        await v.redraw(interaction)


class ReasonSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Why is this needed?", min_values=1, max_values=1, row=3,
                         options=[discord.SelectOption(label=label, value=code, default=code == "no_show")
                                  for code, label in REASONS.items()])

    async def callback(self, interaction):
        self.view.reason_code = self.values[0]
        await self.view.redraw(interaction)


class StageButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Stage", style=discord.ButtonStyle.primary, row=4)

    async def callback(self, interaction):
        v = self.view
        if v.action == "pf_add":
            return await interaction.response.send_modal(PfModal(v))
        if v.action in NEEDS_REASON:
            return await interaction.response.send_modal(ReasonModal(v))
        await v.stage(interaction)


class UndoButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Undo last", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction):
        v = self.view
        if v.ops:
            v.ops.pop()
        await v.redraw(interaction)


class ApplyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Apply", style=discord.ButtonStyle.success, row=4)

    async def callback(self, interaction):
        v = self.view
        if not v.ops:
            return await v.redraw(interaction, "⚠ Nothing staged yet.")
        plan = v.preview()
        if plan.errors:
            return await v.redraw(interaction, "⚠ Fix the problems first.")
        if plan.warnings:
            force = ForceApplyView(v, plan.warnings)
            return await interaction.response.send_message(
                "⚠ **Warnings**:\n" + "\n".join("• " + w for w in plan.warnings)[:1800]
                + "\nYou can still apply the changes.", view=force, ephemeral=True)
        await v.run_apply(interaction)


class ExportButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Export JSON", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction):
        v = self.view
        parties = v.ctx.db.active_slot_parties(v.slot_id)
        files = [discord.File(io.BytesIO(dumps(party_json(v.ctx, p["id"]))), filename=f"party-{p['id']}.json")
                 for p in parties][:10]
        await interaction.response.send_message(
            "Edit the members (and add `reasons` for removed players), then run **/party import** with the file. "
            "The solver's original party is included for reference.", files=files, ephemeral=True)


class CloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Close", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction):
        self.view.stop()
        await interaction.response.edit_message(content="Closed.", view=None)


class ForceApplyView(discord.ui.View):
    def __init__(self, owner, warnings):
        super().__init__(timeout=300)
        self.owner, self.warnings = owner, warnings

    async def interaction_check(self, interaction):
        return str(interaction.user.id) == self.owner.user_id

    @discord.ui.button(label="Apply anyway", style=discord.ButtonStyle.danger)
    async def force(self, interaction, button):
        self.stop()
        await self.owner.run_apply(interaction, warnings=self.warnings)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing changed.", view=None)


class ReasonModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Why?")
        self.owner = view
        self.text = discord.ui.TextInput(required=view.reason_code == "other", max_length=300,
                                         style=discord.TextStyle.paragraph,
                                         placeholder="Required for 'Other'. Be kind: they will see it.")
        self.add_item(discord.ui.Label(text="Reason (the player can read this)", component=self.text))

    async def on_submit(self, interaction):
        v = self.owner
        text = (self.text.value or "").strip()
        if v.reason_code == "other" and not text:
            return await v.redraw(interaction, "⚠ 'Other' needs a written reason. Nothing was staged.")
        await v.stage(interaction, text=text)


class PfModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Party Finder player")
        self.owner = view
        self.name = discord.ui.TextInput(required=False, max_length=party_edit.MAX_PF_NAME,
                                         placeholder="Character name, to track them (optional)")
        self.add_item(discord.ui.Label(text="Name (optional)", component=self.name))

    async def on_submit(self, interaction):
        await self.owner.stage(interaction, name=(self.name.value or "").strip() or None)


class AnyoneModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Someone else")
        self.owner = view
        self.user = discord.ui.UserSelect(min_values=1, max_values=1, placeholder="Anyone in the server")
        self.add_item(discord.ui.Label(text="Player", component=self.user))

    async def on_submit(self, interaction):
        v, user = self.owner, self.user.values[0]
        if getattr(user, "bot", False):
            return await v.redraw(interaction, "⚠ Bots cannot join a party.")
        v.anyone = (str(user.id), user.display_name)
        v.object = str(user.id)
        v.ctx.db.upsert_player(user.id, user.display_name, now=v.ctx.now()) if v.ctx.db.get_player(user.id) is None else None
        await v.redraw(interaction)


async def open_edit_panel(ctx, interaction, party_id):
    if not _is_organizer(ctx, interaction.user):
        return await _reply(interaction, "Only organizers can edit a party.")
    party = ctx.db.party(party_id)
    if party is None or party["status"] not in ("confirmed", "done"):
        return await _reply(interaction, "That party is not active.")
    view = SlotEditView(ctx, party["slot_id"], interaction.user.id, interaction.guild_id)
    view.subject = None
    await interaction.response.send_message(view.render(), view=view, ephemeral=True)


async def _reply(interaction, text):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)
