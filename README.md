# Raid matchmaking bot (V1 skeleton)

A Discord bot for organizer-run raid events. Players sign up with their roles, prog checkpoint and the
slots they can play. Organizers watch a live heatmap, export the signups, run the solver
(`../raid-solver/solve.py`) and import the draft it produces.

**Status:** skeleton. Signup, heatmap, export and import work and are tested without Discord. Swaps, confirm
buttons, votes, standby and the weights live in later steps. See "What is not built yet".

## Run it

1. Create an application at <https://discord.com/developers/applications>, add a Bot, copy its token.
2. Invite it to a test server with the scopes `bot` and `applications.commands` (no special permissions,
   no privileged intents).
3. In this folder (you will need [uv](https://docs.astral.sh/uv/)):

   ```bash
   uv sync
   cp .env.example .env          # then edit .env: set DISCORD_TOKEN, and GUILD_ID for instant command sync
   uv run python -m bot
   ```

   `.env` and the database are git-ignored. Never paste the token anywhere else.
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
| 9 | organizer | `/draft import [day]` with `draft.json`. Without `day` the bot finds the day by matching the draft's fingerprint to the exports. It replaces only that day's draft and refuses a stale file or a slot on another day. Nothing is posted to players. |

Carry-over between days is done by the bot, so the solver is unchanged: each player's `max_per_run` is reduced
by the parties they already have on earlier days of the same run (players with none left are left out of that
day's export with a warning), and "unmatched last time" looks at the previous day that has a draft, falling
back to the previous run.

Config keys: `cutoff_time`, `cutoff_days_before`, `reminder_channel_id` (env `REMINDER_CHANNEL_ID`),
`reminder_interval_minutes`, `organizer_role_id`.

## The fingerprint check

`solve.py` stamps its output with a SHA-256 of the signups file it was given. On import the bot rebuilds the
current signups from its database, hashes them the same way and compares. Any change (a tick, a role, a prog,
a limit, a helper grant) changes the hash, so a stale draft cannot be imported. The export contains nothing
that depends on the clock, so exporting twice without changes gives the same hash. The bot also checks that
every party in the draft matches the signups (known slot and players, ticked slots, roles, party comp).

## Things that must stay in sync with the solver

* `solver_phase_cross_cost` in `config.json` must equal `phase_cross_cost` in `raid-solver/config.json` (3).
  The bot uses it to turn "OK with N checkpoints lower" into the solver's distance units.
* Checkpoints (19 for Ultima Weapon, index 0 = P1 fresh ... 18 = P4 enrage) live in `schema.sql`.

## Layout

```
bot/grid.py        slot grid (America/New_York, DST safe)      bot/solver_io.py  export, import, fingerprint
bot/db.py          SQLite layer                                 bot/heatmap.py    /run status table
bot/handlers.py    command logic (no Discord decorators)        bot/views.py      /availability menu and modal
bot/commands.py    slash command registration                   bot/app.py        client, command sync
schema.sql         tables + the Ultima Weapon checkpoints       tests/            pytest suite
```

## Tests

```bash
uv run pytest
```

They cover the grid (including both DST-change days), the database layer, the export and fingerprint,
draft import and its refusals, the heatmap, and every command handler and menu callback using fake
Discord interactions. One test runs the real `solve.py` and imports its output (skipped if `../raid-solver`
is missing). No test connects to Discord.

## What is not built yet

Swapping people, confirm and decline buttons, standby, votes, "ended early" and play logging, helper
pairings (`helper_seeds` exists but nothing uses it), and the plain-text "why wasn't I matched" message to
players (the solver's reasons are stored in `draft_notes` and shown to organizers on import).

Also not built: automatic solving at the cutoff, and a confirm flow. Until there is one, the carry-over counts
parties from any imported draft (draft or confirmed) on earlier days, so re-importing an earlier day changes
later days' caps. Each day is solved on its own, so a day cannot trade a seat against a later day. The day
dropdown shows at most 25 days (Discord's select limit).
