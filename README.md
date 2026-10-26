# Keep-Dreaming-Bot

Private Discord bot for a friends' server.

## Commands (slash commands, registered only in the configured server)

| Command | Behaviour |
|---|---|
| `/puta` | Shows today's name. If today (Europe/Lisbon date) has no pick yet, one is chosen at random and stored. |
| `/putaindor` | Podium of the 3 most-picked names (a pick = one day's name). |
| `/sync` | Owner-only. Re-syncs the commands to the server (this also happens on startup). |
| `/register nickname` | Joins the in-house roster (starts with a default hidden rating). |
| `/profile view [user]` | In-house rank (or placement progress), League reference rank, role proficiency. |
| `/profile roles role proficiency` | Your own 1-5 proficiency on a role (or clear it). |
| `/profile rank tier [division]` | Self-reported League Solo/Duo rank; seeds the starting rating while you have few in-house games. |
| `/riot link riot_id [platform] [player]` | Links a Riot account (Name#TAG). The first one becomes the main; its Solo/Duo rank seeds the rating while you have few in-house games. Admins can link for others (the channel is told, with the data notice). Needs `RIOT_API_KEY`. |
| `/riot main`, `/riot unlink`, `/riot refresh`, `/riot list` | Change the main account, delete an account and all data tied to it (if it was the main, the earliest-linked remaining account becomes the main), refresh names/rank/ranked history (10 min cooldown for players), list the linked accounts (visible to everyone). |
| `/admin player register member [display_name]` | Allowlisted admins only: registers a server member in advance (identity = their Discord user ID; display name defaults to their current Discord name). Same defaults as `/register`; an already-registered member is left untouched. |
| `/admin rating set|inspect|recompute|rank`, `/admin roles set` | Allowlisted admins only (`ADMIN_USER_IDS`): rating adjustments, diagnostics (the only place raw numbers appear), rank and proficiency overrides. |

A daily task also makes the pick at 00:00 Europe/Lisbon.

In-house ranks (Bot Máximo, Rush Caixa, Duro que nem um rijo, No Box, KD esports) show a player's standing *within the group*; they are not League ranks.

## Layout

```
bot/
  __main__.py   entry point            config.py  .env -> Settings
  app.py        KDBot (loads features) db.py      SQLite + migrations
  features/
    nameday/    service.py (rules, no Discord) / repository.py (SQL) / cog.py (commands) / seed/
    admin/      /sync
    players/    roster + role proficiency; cog.py also wires the rating services and admin.py
    riot/       optional Riot module: client.py (HTTP, rate limit), roles.py (role experience), service.py,
                repository.py, cog.py (/riot); plugs into the players/ratings services, never imported by them
    ratings/    pure rating core (no Discord): rank.py R, matchmaking.py E, ladder.py, engine.py
                (the only openskill import), replay.py; service.py / repository.py
tests/
tools/          dev-only helpers (ladder_sim.py: inspect tier and drift distributions)
```

New features go in `bot/features/<name>/` with a `cog.py` exposing `setup(bot)`, and are added to `EXTENSIONS` in `bot/app.py`.

## Data

SQLite file at `DB_PATH` (default `data/kdbot.sqlite3`, git-ignored). Tables: `names`, `phrases`, `picks` (one row per day); Riot (optional, migration 3): `riot_accounts`, `riot_role_games`; in-house: `players`, `player_roles`, `rating_events` and `rank_signals` (authoritative inputs) and `ratings`, `rating_history` (caches rebuilt by replay). On first run, names and phrases are loaded from `bot/features/nameday/seed/*.json`. Schema changes are versioned in `bot/db.py` (`MIGRATIONS`). To back up, copy the `data/` folder while the bot is stopped. To reset the ranking, stop the bot and delete the `.sqlite3` file (and `-wal`/`-shm` files).

## Local setup

1. Python 3.11+. `python -m venv .venv`, then `.venv\Scripts\activate` (Windows).
2. `pip install -e ".[dev]"`
3. Copy `.env.example` to `.env`; fill in `DISCORD_TOKEN` (Bot tab -> Reset Token) and `GUILD_ID`.
   Optionally set `RIOT_API_KEY` (and `RIOT_DEFAULT_PLATFORM`, default `euw1`) to enable `/riot`; see `docs/riot-policy.md` first.
   Optionally set `ADMIN_USER_IDS` (comma-separated Discord user IDs) to enable the admin commands; empty means nobody.
4. Invite the bot with scopes `bot` + `applications.commands`. No privileged intents are needed.
5. Run from the repo root: `python -m bot`

## In-house ratings in one paragraph

Three quantities per player: **R** (rank-based estimate from the main League account's Solo/Duo rank, or a self-reported/admin rank), **H** (hidden in-house rating, OpenSkill, seeded from R and changed only by confirmed in-house results) and **E** (the matchmaking estimate, `R + D*tanh((H-R)/D)`, so E can never drift more than `d_down`/`d_up` from R). H is replayed from `rating_events` plus confirmed matches, so every cache can be rebuilt (`/admin rating recompute`); E and the visible ladder are computed from stored inputs, so tuning `MatchmakingParams` or `LadderConfig` never rewrites history. Run `python tools/ladder_sim.py --help` to compare parameter sets.

## Checks

```
ruff check . && ruff format --check .
pyright
pytest
```
