# Keep-Dreaming-Bot

Private Discord bot for a friends' server.

## Commands (slash commands, registered only in the configured server)

| Command | Behaviour |
|---|---|
| `/puta` | Shows today's name. If today (Europe/Lisbon date) has no pick yet, one is chosen at random and stored. |
| `/putaindor` | Podium of the 3 most-picked names (a pick = one day's name). |
| `/sync` | Owner-only. Re-syncs the commands to the server (this also happens on startup). |

A daily task also makes the pick at 00:00 Europe/Lisbon.

## Layout

```
bot/
  __main__.py   entry point            config.py  .env -> Settings
  app.py        KDBot (loads features) db.py      SQLite + migrations
  features/
    nameday/    service.py (rules, no Discord) / repository.py (SQL) / cog.py (commands) / seed/
    admin/      /sync
tests/
```

New features go in `bot/features/<name>/` with a `cog.py` exposing `setup(bot)`, and are added to `EXTENSIONS` in `bot/app.py`.

## Data

SQLite file at `DB_PATH` (default `data/kdbot.sqlite3`, git-ignored). Tables: `names`, `phrases`, `picks` (one row per day). On first run, names and phrases are loaded from `bot/features/nameday/seed/*.json`. Schema changes are versioned in `bot/db.py` (`MIGRATIONS`). To back up, copy the `data/` folder while the bot is stopped. To reset the ranking, stop the bot and delete the `.sqlite3` file (and `-wal`/`-shm` files).

## Local setup

1. Python 3.11+. `python -m venv .venv`, then `.venv\Scripts\activate` (Windows).
2. `pip install -e ".[dev]"`
3. Copy `.env.example` to `.env`; fill in `DISCORD_TOKEN` (Bot tab -> Reset Token) and `GUILD_ID`.
4. Invite the bot with scopes `bot` + `applications.commands`. No privileged intents are needed.
5. Run from the repo root: `python -m bot`

## Checks

```
ruff check . && ruff format --check .
pyright
pytest
```
