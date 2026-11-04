# Raid matchmaking bot

Organizer-run FFXIV raid events on Discord. Players sign up with their roles, prog checkpoint and the slots they
can play. Organizers watch a live heatmap, export each day's signups, run the solver (`../raid-solver/solve.py`),
import the draft, ask players to confirm, and approve it, which posts one thread per slot with the parties.
Afterwards players end their party, rate each other, and organizers can swap people in and out.

**Status:** working end to end on a test server (signup, solver round trip, confirmations, party threads, swaps,
ratings, verified characters, FFLogs prog scoring). The dashboard is an early start: login and a read-only run list.
See "What is not built yet".

## How it fits together

```
 Discord  <-->  bot (python -m bot)  <-->  PostgreSQL  <-->  dashboard (FastAPI + Svelte)
                                              ^
                                              |  schema: numbered SQL files, applied by the dashboard
                                              +--- dashboard/server/app/migrations/
```

* **The database is PostgreSQL**, shared by both programs, which may run on different machines.
* **The dashboard owns the schema.** Tables are changed only by adding a numbered migration under
  `dashboard/server/app/migrations/`; the dashboard applies them on startup (or `python -m app.migrate`). The bot
  never creates or alters tables: it checks the database is migrated far enough and refuses to start if not.
* Fights and their checkpoints are data, not schema: the bot syncs them from `prog_points/*.json` on every start.
* The solver (`../raid-solver`) is a separate program that the bot talks to only through JSON files.

> **Warning: the bot's database calls are synchronous.** The bot talks to PostgreSQL with a blocking driver, on the
> same thread that runs Discord's event loop. Each query waits for a network round trip, and one command can run
> many queries (an export, `/draft approve` and `/party dashboard` read a lot). While the bot waits it handles
> nothing else, and Discord wants a command answered within 3 seconds.
>
> * Database on the **same machine or the same local network** (about 1 ms per query): you will not notice.
> * Database **far away** (50 to 200 ms per query, such as a hosted database in another region): heavy commands
>   can run past 3 seconds and show "The application did not respond", everyone else waits meanwhile, and the
>   once-a-minute loop (reminders, snapshots, auto-ending parties) slows down.
> * So: **run the bot and PostgreSQL close together**, and put the distance between the dashboard and the
>   database instead (the dashboard serves each request on its own thread, so it is not affected). Measure before
>   you commit to a setup: `psql "$DATABASE_URL" -c '\timing on' -c 'select 1'` should say a few milliseconds.
> * If the bot ever has to talk to a distant database, the fix is to move the database work off the event loop
>   (an async driver or running handlers' database calls in threads). That is not done yet.
> * If the database is unreachable a command fails with an error; the connection is re-opened once on a dropped
>   connection, not retried further. A database restart is therefore survivable, an outage is not.

## Run it

1. Create an application at <https://discord.com/developers/applications>, add a Bot, copy its token.
2. Invite it to a test server with the scopes `bot` and `applications.commands` (no privileged intents).
   The bot needs these permissions in the channels it posts to (the parties channel and the reminder channel):
   **View Channel**, **Send Messages**, **Create Public Threads** and **Send Messages in Threads**.
   `/draft approve` creates one thread per slot, so without the two thread permissions it can confirm a day
   but cannot post the parties. To remove swapped-out players from a thread it also needs **Manage Threads**.
3. **Start PostgreSQL.** For development, `docker compose up -d` starts PostgreSQL 16 on `localhost:5432` with the
   databases `raidbot` (for the bot and dashboard) and `raidtest` (for pytest); the password in that file is public,
   so it is for local use only. For a real server use your own database with your own credentials and add
   `?sslmode=require` to the URL.
4. **Configure and migrate.** You need [uv](https://docs.astral.sh/uv/) and Python 3.11+.

   ```bash
   uv sync
   cp .env.example .env          # edit it: DISCORD_TOKEN, DATABASE_URL, and GUILD_ID for instant command sync
   cd dashboard/server && pip install -r requirements.txt && python -m app.migrate && cd ../..
   uv run python -m bot
   ```

   `.env` is git-ignored. Never paste the token or the database password anywhere else. The migrate step is also
   done by the dashboard on its own startup; run it by hand when you start the bot first, and again after pulling a
   change that adds a file to `dashboard/server/app/migrations/`. The bot's message when it refuses to start says
   the same.
5. **The dashboard** (optional; the bot does not need it running):

   ```bash
   cd dashboard/server
   cp .env.example .env          # Discord OAuth settings, SESSION_SECRET and the same DATABASE_URL as the bot
   uvicorn app.main:app --port 8000
   ```

   Only organizers (the role in `ORGANIZER_ROLE_ID`) can log in. The Svelte app in `dashboard/web` is only a login
   page and a run list so far, and its `package.json` is not in the repo yet, so it cannot be built until that is added.

### Settings

`config.json` holds the tunables (grid hours, default limits, cutoff times, vote and confirmation windows, ...).
`.env` holds secrets and overrides. People with **Manage Server** are always organizers.

| `.env` key | What |
| --- | --- |
| `DISCORD_TOKEN` | the bot's token (required) |
| `DATABASE_URL` | PostgreSQL URL shared with the dashboard (required), `postgresql://user:password@host:5432/raidbot` |
| `GUILD_ID` | sync slash commands to one server only (instant); empty = global, up to an hour |
| `ORGANIZER_ROLE_ID`, `REMINDER_CHANNEL_ID`, `PARTIES_CHANNEL_ID` | defaults for the organizer role and channels; `/settings` overrides them per server without editing files |
| `FFLOGS_CLIENT_ID`, `FFLOGS_CLIENT_SECRET` | FFLogs API client, needed for character lookups and prog scoring |
| `DEBUG_COMMANDS` | `1` registers the organizer `/debug` testing commands |

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

## Party threads: ending, editing and swapping

Each approved party gets a message in its slot's thread with buttons that keep working after a restart.

* **Party ended** (any member, or an organizer): refused before the slot starts, asks for confirmation, then marks
  the party done, logs the minutes played for burnout tracking and posts the rating button. An organizer can end it
  for everyone or undo an ended party. When a slot's length runs out, the minute loop ends its parties
  automatically.
* **Edit composition** (organizers): a panel to swap a member out, change a role, or export the party as JSON,
  edit it elsewhere and bring it back with `/party import`. A swap needs a reason (did not show up, can't make
  it, role or prog did not fit, other). Replacements come from the standby list, but any Discord member can be
  chosen. The bot warns when the replacement has not signed up, does not list the role, has a prog below the party's target, did not tick the slot, is already in another party of that slot, or would go over their daily or per-run limit; an organizer may force it anyway. The
  swapped-out player is told in the parties channel, removed from the thread and put back on standby, and a
  **Why was I swapped?** button shows them the reason. A player swapped in is asked to confirm too.
* Edits are allowed until the slot closes. `/draft approve` stores the parties exactly as the solver made them
  (`party_originals`) and every later change goes into `party_changes`, so manual edits can be compared with the
  solver's output later (the aim is that most changes end up being just no-shows).

## Characters

Players link verified FFXIV characters (alts allowed, up to 8): `/character add` gives a one-time code to put in
the Lodestone profile, `/character verify` reads the profile and finishes the link, `/character list` and
`/character remove` manage them. The Lodestone id is the identity, so a rename or world transfer does not break the
link. After verification the bot looks the character up on FFLogs and keeps name and world current. A player with
several characters picks which one they bring for each day when signing up, and the choice is frozen in that day's
snapshot. Lodestone has no API, so this reads the public pages and is the fragile part (`bot/lodestone.py`); the
region is `lodestone_region` in `config.json`.

## Prog points and FFLogs scoring

A fight's checkpoints live in `prog_points/<SHORT>.json` (written by the prog point editor) and the bot syncs them
into the database on every start, moving stored numbers (players' prog, snapshots, party targets, scorer results) to
the new numbering by phase and name. Every fight gets a final `Clear` point automatically. The scorer reads a
player's FFLogs reports, works out which prog points they reached and stores the highest. Points tagged `cleanup`
have two modes: `normal` ignores the tag, `consistent` only counts a cleanup point reached in enough of the recent
pulls (3 of 5 unless the file says otherwise). Setting `DEBUG_COMMANDS=1` adds the organizer testing commands
`/debug score` (run the scorer for a member and set their prog), `/debug setprog`, `/debug sync_checkpoints` (re-read
the file now) and `/debug complete` (mark approved slots as ended so voting can start).

## Commands

| Who | Command |
| --- | --- |
| players | `/availability` (roles, prog, slots), `/limits`, `/myschedule`, `/rate`, `/character add \| verify \| list \| remove` |
| organizers | `/run create \| open \| close \| extend \| status`, `/helper grant \| revoke`, `/draft export \| import \| confirm \| approve`, `/party import \| dashboard`, `/votes summary`, `/settings show \| set \| clear` |
| organizers (needs `DEBUG_COMMANDS=1`) | `/debug complete \| setprog \| score \| sync_checkpoints` |

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
bot/                       the Discord bot (python -m bot)
  app.py, commands.py      client, command sync, slash command registration
  handlers.py              command logic without Discord decorators (what the tests call)
  views.py                 /availability menu and modals          party_views.py     party thread buttons
  confirm_views.py         Confirm / Can't make it buttons        dashboard.py       /party dashboard view
  db.py, pg.py             database layer and the psycopg wrapper (`?` placeholders, rows, transactions)
  grid.py                  slot grid (America/New_York, DST safe)  solver_io.py       export, import, fingerprint
  snapshots.py, reminders.py, confirmations.py, votes.py, parties.py, posting.py, heatmap.py, services.py
  characters.py, character_handlers.py, lodestone.py, fflogs.py    characters and FFLogs lookups
  progscore.py, progpoints.py, prog_scorer.py, fflogs_api.py       prog points and the scorer
  seed.py                  fake test players (python -m bot.seed)
dashboard/
  server/app/              FastAPI app: login (Discord OAuth), /api/runs, db.py (pool), migrate.py
  server/app/migrations/   THE SCHEMA: 0001_baseline.sql, 0002_..., one file per change
  server/tests/            tests for the migration runner
  web/                     Svelte app (login page and run list; no package.json yet)
prog_points/               prog point files, one per fight
tests/                     pytest suite for the bot
docker-compose.yml         local PostgreSQL for development and tests
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
to match), `--helpers N`, `--slot KEY` (repeatable, to tick only some slots), `--guild-id`, `--database-url URL`.
Create the run first (`/run create`) and seed **before the day's cutoff**: after it a day's signups are frozen in
a snapshot, and the script skips days that have one. The script can run while the bot is running; run it again
to reset the fakes. Their ids are 19-digit numbers above 9e18 that no real account can have, and they are not
real server members, so posted parties show them as unknown-user mentions. You can rate them yourself (the
rating menu takes names from the database), but they cannot rate you or each other. To see `/votes summary`
(which hides anyone with fewer than 3 votes) you need 3 real voters, or votes added by hand to the `votes` table.

## The database schema (owned by the dashboard)

All tables live in numbered SQL files in `dashboard/server/app/migrations/`, applied in order by
`dashboard/server/app/migrate.py` and recorded in `schema_migrations` with a checksum (an applied file that was
edited is refused). Each file runs in its own transaction, and two dashboards starting at once are safe (advisory
lock). The bot only checks that the database reaches `REQUIRED_SCHEMA_VERSION` in `bot/db.py`.

**To change the schema:**

1. Add the next file, `000N_short_name.sql`. Never edit one that has been applied anywhere.
2. Keep the change additive while the bot may be older than the dashboard: new tables, new columns that are
   nullable or have a default. To drop or rename something, first ship a bot that no longer uses it, then migrate.
3. In the same change that makes the bot use the new table or column, raise `REQUIRED_SCHEMA_VERSION` in
   `bot/db.py`. An older bot then refuses to start on an old database and tells you to migrate, instead of
   failing on a missing column halfway through a command.
4. Deploy the dashboard (or run `python -m app.migrate`) before starting the new bot.

Conventions: timestamps are UTC ISO-8601 strings in TEXT columns (they sort correctly as text), flags are INTEGER
0/1, Discord ids are TEXT, and run names are unique per server ignoring case. Queries in the bot are written with
`?` placeholders and translated by `bot/pg.py`. **Back up with `pg_dump`** before a migration you are unsure about:
the migration runner rolls a failed file back, but it cannot undo one that succeeded.

## Tests

```bash
docker compose up -d     # PostgreSQL for the tests (or set TEST_DATABASE_URL to any empty database)
uv run pytest
```

Each test gets its own schema with the dashboard's migrations applied, so tests cannot see each other's data and
nothing is left behind; the whole suite takes about half a minute because of that. The suite covers the grid
(including both DST-change days), the database layer and its Postgres wrapper, the export and fingerprint, draft
import and its refusals, confirmations, party actions and swaps, the heatmap, characters, prog scoring, and every
command handler and menu callback using fake Discord interactions, plus the dashboard's migration runner
(`dashboard/server/tests`). One test runs the real `solve.py` and imports its output (skipped if `../raid-solver`
is missing). No test connects to Discord.

## What is not built yet

* The bot and the database must be run close together, because database calls block the event loop (see the warning
  at the top).
* Helper pairings: `helper_seeds` exists but nothing uses it.
* A way to set `actual_minutes` when a slot ends early (the votes and auto-end already honour it when set).
* The plain-text "why wasn't I matched" message to players (the solver's reasons are stored in `draft_notes` and
  shown to organizers on import).
* Automatic solving at the cutoff. Each day is solved on its own, so a day cannot trade a seat against a later day.
* The dashboard beyond login and a read-only list of runs (the party status view is still the `/party dashboard`
  command), and the dashboard's `package.json`.
* Copying data from the old SQLite file into PostgreSQL (the bot used SQLite before; no import tool exists).
* The day dropdown shows at most 25 days (Discord's select limit).
