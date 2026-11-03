# Raid matchmaking bot (V1 skeleton)

A Discord bot for organizer-run raid events. Players sign up with their roles, prog checkpoint and the
slots they can play. Organizers watch a live heatmap, export the signups, run the solver
(`../raid-solver/solve.py`) and import the draft it produces.

**Status:** skeleton. Signup, heatmap, export and import work and are tested without Discord. Swaps, standby and
the weights live in later steps. See "What is not built yet".

## Run it

1. Create an application at <https://discord.com/developers/applications>, add a Bot, copy its token.
2. Invite it to a test server with the scopes `bot` and `applications.commands` (no privileged intents).
   The bot needs these permissions in the channels it posts to (the parties channel and the reminder channel):
   **View Channel**, **Send Messages**, **Create Public Threads** and **Send Messages in Threads**.
   `/draft approve` creates one thread per slot, so without the two thread permissions it can confirm a day
   but cannot post the parties.
3. In this folder (you will need [uv](https://docs.astral.sh/uv/)):

   ```bash
   uv sync
   cp .env.example .env          # then edit .env: set DISCORD_TOKEN, and GUILD_ID for instant command sync
   uv run python -m bot
   ```

   `.env` and the database are git-ignored. Never paste the token anywhere else.

   **The dashboard owns the database schema, so the bot cannot create it.** The bot refuses to start on a
   database that has not been migrated far enough. Migrate it first, either by starting the dashboard (it
   migrates on startup) or by hand (standard library only, nothing to install):

   ```bash
   cd dashboard/server && python -m app.migrate        # creates or upgrades the file the bot will use
   ```

   Do this again after pulling a change that adds a file to `dashboard/server/app/migrations/`. Both programs
   must point at the same file: leave `DB_PATH` unset (default `raidbot.sqlite3` in the repo root, for the bot
   and the dashboard alike) or give both the same value. A relative path means relative to the repo root.
4. Optional settings: `config.json` (grid hours, default limits, organizer role id) and `.env`
   (`ORGANIZER_ROLE_ID`, `DB_PATH`). People with **Manage Server** are always organizers.

## A run, start to finish

A run spans several days, but **each day has its own signup cutoff** (a fixed clock time, the day before by
default) and its own export, solve and import. Players cannot plan a whole week and their prog changes daily.
Nothing is solved automatically: at each cutoff the bot nags the organizers until `/draft export` is run.

| Step | Who | What |
| --- | --- | --- |
| 1 | organizer | `/run create name content start_date end_date [cutoff_time] [cutoff_days_before] [earliest_start] [latest_start] [timezone]` makes a draft run, its slot grid and one cutoff per day. Default: each day closes at `cutoff_time` (config, `23:59`) `cutoff_days_before` (config, `1`) days earlier, in the run's timezone, so Monday closes Sunday 23:59. A run may start tomorrow with tonight as the first cutoff. A day already past its cutoff when the run opens starts closed. The create is refused if a cutoff is not before that day's first slot. |
| 2 | organizer | `/helper grant @person` for each helper (once per fight). |
| 3 | organizer | `/run open` starts signups. Only one run can be open at a time. |
| 4 | players | `/availability`: pick roles, prog checkpoint (shown prominently, since it changes), then a day and its slots. Each day shows when it closes; a closed day is read-only. The **Limits** button sets max per day, max per run and how many checkpoints lower they accept. `/limits` and `/myschedule` show the same. Roles, prog and limits stay editable while any day is open. |
| 5 | organizer | `/run status [day]`: each day's state (open, closed, exported) and cutoff, plus ticks per slot split into tanks, healers and DPS with a marker where the role mix could fill a party. |
| 6 | bot | At a day's cutoff the bot posts in `reminder_channel_id` (pinging `organizer_role_id` if set) and repeats every `reminder_interval_minutes` (30) until that day is exported or its first slot has started. State is kept in the database, so a restart neither repeats nor forgets. With no channel configured the bot logs a warning and `/run create` says so. |
| 7 | organizer | `/draft export [day]`: the earliest closed day that is not exported yet. An open day is refused unless `force:true` (a forced early export does not stop the reminders). The bot attaches `<run>-<day>-signups.json` and saves it in `exports/`. `/run close [day]` closes a day now (no day: every remaining day) and `/run extend day new_cutoff` moves a cutoff. |
| 8 | whoever runs the solver | `python ../raid-solver/solve.py exports/<file>.json -o draft.json` (unchanged solver). |
| 9 | organizer | `/draft import [day]` with `draft.json`. Without `day` the bot finds the day by matching the draft's fingerprint to the exports. It replaces only that day's draft and refuses a stale file or a slot on another day. Nothing is posted to players yet. |
| 10 | organizer | `/draft approve [day]` marks that day's draft parties `confirmed` and posts them in `parties_channel_id`: one thread per slot, one message per party (members as mentions with their role, the target checkpoint, helper and "placed below their prog" marks, Discord timestamps). Refused if the day has no draft or is already approved. With no channel configured (or one the bot cannot find) the day is still confirmed and you are told, and players see their party under `/myschedule`. If some posts fail, running it again posts only what is missing. A confirmed day can no longer be replaced by an import. |
| 11 | players | When a slot ends, the bot (in its once-a-minute loop) posts once in that slot's thread a "Rate your party" button per party. See Votes below. |

Carry-over between days is done by the bot, so the solver is unchanged: each player's `max_per_run` is reduced
by the parties they already have on earlier days of the same run (players with none left are left out of that
day's export with a warning), and "unmatched last time" looks at the previous day that has approved parties,
falling back to the previous run. Only **confirmed** parties count: if an earlier day still has an unapproved
draft, the export says its parties are not counted (and which `/draft approve` to run).

### Confirmations and the status dashboard

* **Before approval:** `/draft confirm [day]` posts one message per slot in the parties channel, pinging that slot's
  draft members, with **I'll be there** and **Can't make it** buttons. The message is edited as answers come in
  (✅ confirmed, ⏳ waiting, ❌ can't make it). Running it again pings only the people who have not answered and
  replaces the old message.
* **Can't make it** asks for a reason, **unavailable at this slot** by default: it unticks that slot so the next
  `/draft export` leaves the player out of it. Other reasons: *unavailable the whole day* (unticks the day), *don't
  want this role or party* (ticks are kept; the organizers are told) and *other* (free text; unticks the slot). The
  bot says in the channel what happened, pinging the organizer role. To fix the party the organizer re-exports, runs
  the solver and runs `/draft import` as usual.
* **Confirmations live per slot and player, not per party**, so a new draft keeps them for everyone still seated in
  the same slot. A new import forgets declines and the rows of players who are no longer seated.
* **After approval:** the slot thread gets the same message for everyone who has not confirmed (nobody is pinged
  if all did). `confirm_reminder_hours` (default 3) before the slot, the minute loop pings the still-unconfirmed
  once more. A decline in the thread tells the organizers to swap the player out with **Edit composition**; a
  player swapped in is asked to confirm too.
* **`/party dashboard [day]`** (organizers, private): a day picker, an "only needing attention" filter and a party
  picker with a detail page (members and their confirmation, an **Open thread** link, **Nudge who hasn't answered**).
  A party needs attention when: a draft has a declined player; an approved party has a declined player, is not
  posted in a thread, has a thread the bot cannot open, has not-confirmed players within the reminder window, or
  has fewer than the full party. 🔴 urgent (approved party), 🟠 needs a decision, 🟡 informational.
* The solver is unchanged and knows nothing about confirmations, so a re-run may move someone who had confirmed.
  Their confirmation stays valid only if they land in the same slot.

### Snapshots: a day's signups are frozen at its cutoff

Roles, prog and limits belong to the player, not to a day. Without a snapshot, a player editing their profile
for Wednesday after Monday's cutoff would change Monday's export and Monday's draft would be refused as stale.
So at each day's cutoff the bot saves a **snapshot** (`day_snapshots`) of every player who ticked a slot that
day: display name, roles, prog and their effective limits (defaults included).

* The once-a-minute loop takes due snapshots (cutoff passed, none yet), also when no reminder channel is set.
  `/run close [day]` takes the snapshot of each day it closes. A day nobody signed up for still gets the marker
  (`run_days.snapshot_utc`), so taking it again is a no-op. A snapshot is never overwritten.
* `/draft export`, `/draft import` and `/run status` take a missing snapshot of a closed day on the spot. It is
  flagged **late** (so is one the loop took more than 5 minutes after the cutoff, for example after downtime),
  and the export then warns: "Snapshot taken late: profile edits between the cutoff and now may be included."
* A day's export and import read its snapshot for name, roles, prog and limits; ticks stay live (they cannot
  change after the cutoff). The remaining per-run cap is the snapshot's `max_per_run` minus the parties the
  player has on earlier days at export time. A forced export of a still-open day has no snapshot and uses live
  data, and the whole-run export without `day` stays live.
* `/run extend` on a day drops its snapshot (the day reopens); a new one is taken at the new cutoff, and an
  earlier export of that day is stale by then.
* **Helper grants are not snapshotted.** Organizers grant helpers on purpose, so a `/helper grant` or `revoke`
  after the cutoff still changes the export and makes an older draft stale.
* Players see a line after changing roles, prog or limits when a day they signed up for is already frozen
  ("... saved at their cutoffs. This change applies from <first open day>."), and `/myschedule` lists the prog
  locked in for each such day. Days exported before this feature existed have no snapshot and stay live.

Config keys: `cutoff_time`, `cutoff_days_before`, `reminder_channel_id` (env `REMINDER_CHANNEL_ID`),
`reminder_interval_minutes`, `organizer_role_id`, `parties_channel_id` (env `PARTIES_CHANNEL_ID`),
`vote_window_hours` (72), `vote_expiry_days` (90), `vote_floor` (3), `confirm_reminder_hours` (3).

### Votes: rating the people you played with

* **Who can vote:** two players who were in the same confirmed party of a slot that has ended (its start plus
  `minutes`, or `actual_minutes` if the slot ended early), within `vote_window_hours` after the end. Never yourself.
* **How:** press the button in the slot's thread, or use `/rate` if that message is lost. Both open an
  ephemeral (only-you) view: pick a member, press Like, Neutral or Dislike (saved at once, the current rating is
  shown), Done. Nothing about votes is ever posted publicly and no counts are shown to players.
* **Storage:** the `votes` table is append-only; the latest row per (voter, target) is the current vote.
  Neutral is stored, so changing your mind back to neutral clears an earlier like or dislike.
* **Into the solver:** the day export has a `votes` list of `{"from", "to", "vote": "like"|"dislike"}`, which
  the solver turns into a bonus for liked pairs and a penalty for disliked ones. For each (voter, target) the
  bot takes the latest vote created **at or before the day's snapshot time**, drops neutral, drops votes older
  than `vote_expiry_days`, and keeps only pairs where both players are in that export. A vote cast after the
  cutoff therefore never changes that day's export, fingerprint or import. (A forced export of a still-open
  day reads votes as of now, like the rest of its live data.) The solver counts every entry, and a dislike on
  a pair beats likes, so a pair that voted both ways counts twice.
* **Organizers:** `/votes summary` shows likes and dislikes received per player in the expiry window, hides
  anyone with fewer than `vote_floor` votes, and names no voters.

**Privacy, honestly.** Players never see anyone's votes, and organizers using the bot see aggregates only.
But the exported signups file contains the raw `from`/`to` votes, because the solver reads them. Whoever holds
an export (it is also saved in `exports/` on the bot's machine) or runs the solver can see who voted for whom,
and `/votes summary` totals are small enough that an organizer who knows a party may still guess. Hiding the
direction would need a small solver change (an aggregated pair format instead of per-voter entries). That is
left for you to decide.

## The fingerprint check

`solve.py` stamps its output with a SHA-256 of the signups file it was given. On import the bot rebuilds the
current signups from its database, hashes them the same way and compares. Any change (a tick, a role, a prog,
a limit, a helper grant) changes the hash, so a stale draft cannot be imported. The export contains nothing
that depends on the clock, so exporting twice without changes gives the same hash. The bot also checks that
every party in the draft matches the signups (known slot and players, ticked slots, roles, party comp).

## Things that must stay in sync with the solver

* `solver_phase_cross_cost` in `config.json` must equal `phase_cross_cost` in `raid-solver/config.json` (3).
  The bot uses it to turn "OK with N checkpoints lower" into the solver's distance units.
* Fights and their checkpoints come from `prog_points/<SHORT>.json` (the files the prog point editor writes), not from SQL. On every start the bot creates the fight if it is new and, when a file changed, replaces its checkpoints and moves stored numbers (players' prog, signup snapshots, party targets, scorer results) to the new numbering by phase and name. Every fight gets a final `Clear` point (a kill) automatically. An optional `display_name` in the file is the name players see.

## Layout

```
bot/grid.py        slot grid (America/New_York, DST safe)      bot/solver_io.py  export, import, fingerprint
bot/db.py          SQLite layer                                 bot/heatmap.py    /run status table
bot/handlers.py    command logic (no Discord decorators)        bot/views.py      /availability menu and modal
bot/commands.py    slash command registration                   bot/app.py        client, command sync
dashboard/server/app/migrations/   the schema (dashboard-owned)  tests/            pytest suite
bot/seed.py        fake test players (python -m bot.seed)
```

## Testing alone with fake players

A full party needs 8 people (2 tanks, 2 healers, 4 DPS) on the same slot. To try the whole flow with just your
own account, seed 7 fake players into the bot's database and be the eighth yourself:

```bash
uv run python -m bot.seed --run "test1"              # 7 fakes: 2 tanks, 2 healers, 3 DPS (one is a helper), you play dps
uv run python -m bot.seed --run "test1" --you tank   # you play tank, the fakes fill the other seats
uv run python -m bot.seed --remove                   # delete every fake player and all their rows
```

Options: `--count N` (more fakes, in whole parties), `--prog N` (their checkpoint, default 0 = P1 fresh; set yours
to match), `--helpers N`, `--slot KEY` (repeatable, to tick only some slots), `--guild-id`, `--db FILE`.
Create the run first (`/run create`) and seed **before the day's cutoff**: after it a day's signups are frozen in
a snapshot, and the script skips days that have one. The script can run while the bot is running; run it again
to reset the fakes. Their ids are 19-digit numbers above 9e18 that no real account can have, and they are not
real server members, so posted parties show them as unknown-user mentions. You can rate them yourself (the
rating menu takes names from the database), but they cannot rate you or each other. To see `/votes summary`
(which hides anyone with fewer than 3 votes) you need 3 real voters, or votes added by hand to the `votes` table.

## The database schema (owned by the dashboard)

All tables live in numbered SQL files in `dashboard/server/app/migrations/`, applied in order by
`dashboard/server/app/migrate.py` and recorded in `schema_migrations` (with a checksum, so an applied file that
was edited is refused). The bot only checks that the database reaches `REQUIRED_SCHEMA_VERSION` in `bot/db.py`.

To change the schema: add the next `000N_name.sql` (never edit an applied one), keep it additive while the bot
may be older than the dashboard, and raise `REQUIRED_SCHEMA_VERSION` in the same change that makes the bot use the
new table or column. Fights and checkpoints are still data the bot syncs from `prog_points/*.json` on start.

## Tests

```bash
uv run pytest
```

The suite also covers the dashboard's migration runner (`dashboard/server/tests`). Every test database gets the
dashboard's migrations applied first, exactly as a real start would find them.

They cover the grid (including both DST-change days), the database layer, the export and fingerprint,
draft import and its refusals, the heatmap, and every command handler and menu callback using fake
Discord interactions. One test runs the real `solve.py` and imports its output (skipped if `../raid-solver`
is missing). No test connects to Discord.

## What is not built yet

Swapping people, decline buttons, standby, a way to set `actual_minutes` when a slot ends early, play logging, helper
pairings (`helper_seeds` exists but nothing uses it), and the plain-text "why wasn't I matched" message to
players (the solver's reasons are stored in `draft_notes` and shown to organizers on import).

Also not built: automatic solving at the cutoff. Each day is solved on its own, so a day cannot trade a seat against a later day. The day
dropdown shows at most 25 days (Discord's select limit).
