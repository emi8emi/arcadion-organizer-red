"""The party status dashboard: which parties and threads need an organizer's attention.

`/party dashboard` opens an ephemeral view with a day picker, a "needing attention" filter, a party picker with a
detail page (members and their confirmation, a link to the thread, a button to nudge who has not answered) and a
refresh button. The checks are in `party_issues`/`collect`, the Discord view below them.
"""
import logging
from datetime import timedelta

import discord

from . import confirmations as cf
from .confirmations import MARKS
from .db import parse_iso
from .services import day_label
from .votes import slot_end

log = logging.getLogger("raidbot")

URGENT, WARN, INFO = 3, 2, 1
DOT = {URGENT: "🔴", WARN: "🟠", INFO: "🟡", 0: "🟢"}


def party_issues(ctx, party, members, slot, have, now, thread_ok=True):
    """[(level, text)] for one party. `have` is the slot's confirmations {discord_id: row}. Level 2 or more
    means the organizer has something to do."""
    out = []
    want = sum(ctx.cfg["party"].values())
    ids = [m["discord_id"] for m in members]
    declined = [d for d in ids if d in have and have[d]["status"] == "declined"]
    pending = [d for d in ids if d not in have or have[d]["status"] == "pending"]
    start = parse_iso(slot["start_utc"])
    soon = start - now <= timedelta(hours=ctx.cfg.get("confirm_reminder_hours", 3))
    approved = party["status"] in ("confirmed", "done")
    if party["status"] == "done":
        return out
    if len(ids) != want:
        out.append((WARN, f"{len(ids)} of {want} seats filled."))
    for d in declined:
        r = have[d]
        why = cf.cancel_label(r["reason_code"], r["reason_text"])
        out.append((URGENT if approved else WARN,
                    f"<@{d}> can't make it ({why}). " + ("Swap them out." if approved
                                                          else "Re-export, run the solver and /draft import.")))
    if approved:
        if not slot["thread_id"] or not party["message_id"]:
            out.append((URGENT, "Not posted in a thread yet. Run /draft approve again."))
        elif not thread_ok:
            out.append((URGENT, "Its thread cannot be found or opened."))
        if pending:
            late = start > now and soon
            out.append((URGENT if late else INFO,
                        f"{len(pending)} not confirmed" + (f", starts {discord.utils.format_dt(start, 'R')}." if late else ".")))
    else:
        asked = [d for d in ids if d in have]
        if not asked:
            out.append((INFO, "Confirmation not requested yet (/draft confirm)."))
        elif pending:
            out.append((INFO, f"Waiting for {len(pending)} answer(s)."))
    return out


async def collect(ctx, run, day):
    """Every party of the day with its members, confirmations, issues and thread link."""
    now, rows, thread_state = ctx.now(), [], {}
    for slot in ctx.db.slots_on(run["id"], day):
        have = cf.statuses(ctx.db, slot["id"])
        for party, members in cf.slot_parties(ctx.db, slot["id"]):
            ok = True
            if party["status"] in ("confirmed", "done") and slot["thread_id"]:
                if slot["id"] not in thread_state:
                    thread_state[slot["id"]] = await cf._channel(ctx, slot["thread_id"]) is not None
                ok = thread_state[slot["id"]]
            issues = party_issues(ctx, party, members, slot, have, now, ok)
            ids = [m["discord_id"] for m in members]
            rows.append({"slot": slot, "party": party, "members": members, "have": have, "issues": issues,
                         "level": max((lvl for lvl, _ in issues), default=0),
                         "counts": cf.counts(ctx.db, slot["id"], ids),
                         "url": (f"https://discord.com/channels/{run['guild_id']}/{slot['thread_id']}"
                                 if slot["thread_id"] else None)})
    return rows


def days_with_parties(ctx, run):
    return [r["day"] for r in ctx.db.conn.execute(
        """SELECT DISTINCT substr(s.slot_key, 1, 10) AS day FROM parties p JOIN slots s ON s.id = p.slot_id
           WHERE p.run_id = ? ORDER BY day""", (run["id"],))]


def row_label(row):
    p, s = row["party"], row["slot"]
    return f"{s['slot_key'][11:16]} · Party {p['party_index'] + 1}"


def row_line(row):
    c, p = row["counts"], row["party"]
    state = {"draft": "draft", "confirmed": "approved", "done": "ended"}[p["status"]]
    return (f"{DOT[row['level']]} **{row_label(row)}** · {state} · "
            f"{MARKS['confirmed']}{c['confirmed']} {MARKS['pending']}{c['pending'] + c['unasked']} "
            f"{MARKS['declined']}{c['declined']}")


def overview_text(run, day, rows, only_attention):
    shown = [r for r in rows if r["level"] >= WARN] if only_attention else rows
    needs = sum(r["level"] >= WARN for r in rows)
    lines = [f"**{run['name']}** · {day_label(day)} · {len(rows)} part{'y' if len(rows) == 1 else 'ies'}, "
             f"{needs} needing attention" + (" (showing only those)" if only_attention else "")]
    for r in shown:
        lines.append(row_line(r))
        lines += [f"　{DOT[lvl]} {text}" for lvl, text in sorted(r["issues"], reverse=True) if lvl >= WARN or not only_attention][:4]
    if not shown:
        lines.append("Nothing needs attention. 🎉" if rows else "No parties on this day.")
    return "\n".join(lines)[:1990]


def detail_text(run, row):
    lines = [row_line(row)]
    order = {"tank": 0, "healer": 1, "dps": 2}
    for m in sorted(row["members"], key=lambda m: (order[m["role"]], m["discord_id"])):
        have = row["have"].get(m["discord_id"])
        lines.append(f"{MARKS[have['status'] if have else None]} <@{m['discord_id']}> {m['role']}"
                     + (f" · {cf.cancel_label(have['reason_code'], have['reason_text'])}"
                        if have and have["status"] == "declined" else ""))
    lines += [f"{DOT[lvl]} {text}" for lvl, text in sorted(row["issues"], reverse=True)]
    return "\n".join(lines)[:1990]


# ---------------------------------------------------------------- the view

class DashboardView(discord.ui.View):
    def __init__(self, ctx, run, days, day, user_id):
        super().__init__(timeout=900)
        self.ctx, self.run, self.days, self.day, self.user_id = ctx, run, days, day, user_id
        self.only_attention, self.selected, self.rows = False, None, []

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This dashboard belongs to the organizer who opened it.", ephemeral=True)
            return False
        return True

    async def load(self):
        self.rows = await collect(self.ctx, self.run, self.day)
        self._build()

    def current(self):
        return next((r for r in self.rows if r["party"]["id"] == self.selected), None)

    def text(self):
        row = self.current()
        return detail_text(self.run, row) if row else overview_text(self.run, self.day, self.rows, self.only_attention)

    def _build(self):
        self.clear_items()
        self.add_item(DaySelect(self.days, self.day))
        ordered = sorted(self.rows, key=lambda r: (-r["level"], r["slot"]["start_utc"], r["party"]["party_index"]))[:24]
        if ordered:
            self.add_item(PartySelect(ordered, self.selected))
        self.add_item(ToggleButton(self.only_attention, back=self.selected is not None))
        self.add_item(RefreshButton())
        row = self.current()
        if row:
            self.add_item(NudgeButton())
            if row["url"]:
                self.add_item(discord.ui.Button(label="Open thread", url=row["url"]))

    async def show(self, interaction):
        await self.load()
        await interaction.response.edit_message(content=self.text(), view=self)


class DaySelect(discord.ui.Select):
    def __init__(self, days, day):
        super().__init__(placeholder="Day", options=[discord.SelectOption(label=day_label(d), value=d, default=(d == day))
                                                       for d in days[:25]])

    async def callback(self, interaction):
        self.view.day, self.view.selected = self.values[0], None
        await self.view.show(interaction)


class PartySelect(discord.ui.Select):
    def __init__(self, rows, selected):
        super().__init__(placeholder="Open a party (needing attention first)", options=[
            discord.SelectOption(label=f"{DOT[r['level']]} {row_label(r)}"[:100], value=str(r["party"]["id"]),
                                 default=(r["party"]["id"] == selected)) for r in rows])

    async def callback(self, interaction):
        self.view.selected = int(self.values[0])
        await self.view.show(interaction)


class ToggleButton(discord.ui.Button):
    def __init__(self, only, back):
        super().__init__(label="Back to the list" if back else ("Show all" if only else "Only needing attention"),
                         style=discord.ButtonStyle.secondary)
        self.back = back

    async def callback(self, interaction):
        if self.back:
            self.view.selected = None
        else:
            self.view.only_attention = not self.view.only_attention
        await self.view.show(interaction)


class RefreshButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Refresh", style=discord.ButtonStyle.primary)

    async def callback(self, interaction):
        await self.view.show(interaction)


class NudgeButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Nudge who hasn't answered", style=discord.ButtonStyle.danger)

    async def callback(self, interaction):
        view = self.view
        row = view.current()
        await interaction.response.defer(ephemeral=True)
        note = await cf.nudge_slot(view.ctx, row["slot"]["id"])
        await interaction.followup.send(note, ephemeral=True)


async def open_dashboard(ctx, interaction, run, day=None):
    """Send the dashboard (the handler checked that the user is an organizer). Returns False when the run has no parties."""
    days = days_with_parties(ctx, run)
    if not days:
        return False
    view = DashboardView(ctx, run, days, day if day in days else days[0], interaction.user.id)
    if day not in days:                                   # open on the first day that needs something
        for d in days:
            if any(r["level"] >= WARN for r in await collect(ctx, run, d)):
                view.day = d
                break
    await view.load()
    await interaction.response.send_message(view.text(), view=view, ephemeral=True)
    return True
