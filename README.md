# Keep-Dreaming-Bot

Private Discord bot for a friends' server.

## Commands (guild-only slash commands)

| Command | Behaviour |
|---|---|
| `/puta` | Shows today's selected name. If none is valid for today (Europe/Lisbon date), picks a random name from `names.json`, increments its count in `name_stats.json`, stores it with a random phrase in `name_of_the_day.json`. |
| `/putaindor` | Embed with the top 3 names by selection count (`name_stats.json`). |

A background task (every 5 minutes) also picks the day's name if none is set for today.
Owner-only prefix command `/sync` re-syncs the slash commands; commands are also synced to `GUILD_ID` on startup.

## Data (JSON files in `DATA_DIR`, default `./bot/data`)

`names.json`, `phrases.json` (`{name}` placeholder), `name_stats.json` (name → times selected), `name_of_the_day.json` (current pick). Files are written directly by the bot and survive restarts. Take a backup before editing by hand.

## Local setup

1. Python 3.11+ (developed on 3.13). `python -m venv .venv`, then `.venv\Scripts\activate` (Windows).
2. `pip install -r requirements-dev.txt`
3. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN` and `GUILD_ID` (your dev server).
4. For development, copy the data so tracked files stay untouched: `xcopy /E /I bot\data dev_data` and keep `DATA_DIR=./dev_data`.
5. In the Discord Developer Portal → Bot: enable **Message Content Intent**; invite with scopes `bot` + `applications.commands`.
6. Run from the repo root: `python -m bot`
7. Tests: `python -m pytest`

Never commit `.env`.
