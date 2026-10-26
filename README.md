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

| Step | Who | What |
| --- | --- | --- |
| 1 | organizer | `/run create name content start_date end_date signup_deadline [earliest_start] [latest_start] [timezone]` makes a draft run and its slot grid. Use `earliest_start`/`latest_start` (for example 17:00 and 20:00) to offer fewer, busier slots. `timezone` a Iana timezone (e.g., America/New_York) to help create the run in the right time. |
| 2 | organizer | `/helper grant @person` for each helper (once per fight). |
| 3 | organizer | `/run open` starts signups. Only one run can be open at a time. |
| 4 | players | `/availability`: pick roles, prog checkpoint, then a day and its slots. Each click saves. The **Limits** button sets max per day, max per run and how many checkpoints lower they accept. `/limits` and `/myschedule` show the same. |
| 5 | organizer | `/run status [day]`: ticks per slot split into tanks, healers and DPS, with a marker where the role mix could fill a party. Use it to add or drop slots while signups are open. |
| 6 | organizer | `/run close`, then `/draft export`. The bot attaches `<run>-signups.json` and also saves it in `exports/`. |
| 7 | whoever runs the solver | `python ../raid-solver/solve.py exports/<run>-signups.json -o draft.json` |
| 8 | organizer | `/draft import` with `draft.json`. The bot refuses it if the signups changed since the export. A good draft is stored as `draft` parties and nothing is posted to players. |

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
