# Self-hosting

The bot is a single long-running process: `python -m bot`. It needs outbound internet access (Discord,
and optionally Riot and Data Dragon), no open port and no web address, and a place to keep one SQLite
file. Anything that can run a Python worker works: a VPS, a Raspberry Pi, Docker, or a platform such as
Railway or Fly.io.

## Requirements

- Python 3.11 or newer (`.python-version` pins the version the project is developed on).
- A Discord application with a bot user, invited to your server with the scopes `bot` and
  `applications.commands`. No privileged intents are needed.
- Optionally a [Riot API key](https://developer.riotgames.com) for the `/riot` commands (see
  [riot-data.md](riot-data.md)).

## Install and run

```bash
git clone https://github.com/PM-Delgado/Keep-Dreaming-Bot.git
cd Keep-Dreaming-Bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # or: pip install -e .
cp .env.example .env               # then fill in DISCORD_TOKEN and GUILD_ID
python -m bot
```

On start-up the bot creates the database, applies pending migrations and syncs the slash commands to the
server in `GUILD_ID`.

`requirements.txt` and `pyproject.toml` list the same dependencies (a test keeps them equal). Platforms
that install from `requirements.txt` therefore work without further setup.

## Settings

Everything is read once from environment variables or `.env` (`bot/config.py`); the full list with
defaults is in the [README](../README.md#configuration) and in `.env.example`. On a hosting platform set
them as service variables instead of shipping a `.env`, and keep `DISCORD_TOKEN` and `RIOT_API_KEY`
secret. For a production deployment:

- set `ADMIN_USER_IDS` to the Discord user IDs that may run admin commands;
- leave `ENABLE_MOCK_PLAYERS` off;
- optionally set `SCRIM_CHANNEL_ID` and `SCRIM_ROLE_ID` to limit lobbies to one channel and tag a role.

## The database

All state is one SQLite file at `DB_PATH` (default `data/kdbot.sqlite3`, plus `-wal` and `-shm` files while
the bot runs). On a platform with an ephemeral file system, mount a persistent volume and point `DB_PATH`
into it (for example `/data/kdbot.sqlite3`), otherwise every redeploy starts from an empty database.

Schema changes are versioned migrations in `bot/db.py`, applied automatically at start-up. Back up before
updating to a new version, because a migration cannot be undone.

Champion and role icons are Discord *application emojis*, which belong to one Discord application. If you
start a database that was made with a different bot, the first sync replaces every saved icon id the new
bot does not own and uploads the images again (a few minutes).

### Backups

Never copy `kdbot.sqlite3` while the bot is running: the newest writes may still be in the `-wal` file.
Either stop the bot first, or let SQLite make a consistent snapshot:

```bash
python -I -c "
import sqlite3
src = sqlite3.connect('file:data/kdbot.sqlite3?mode=ro', uri=True)
dst = sqlite3.connect('data/backup.sqlite3')
src.backup(dst)
"
```

Copy `backup.sqlite3` somewhere safe and delete it from the server. Restores are the reverse: stop the
bot, put the file at `DB_PATH` and start it again.

### Starting from a prepared roster

To register a whole group before the first start, see the [roster tool](roster-tool.md): build the
database locally and copy it to the volume once. After that the volume is the source of truth.

## Updates and restarts

A restart is a short outage (the old process stops before the new one starts, so there are never two
bots on one token). The bot closes cleanly on `SIGTERM`, and lobby buttons keep working afterwards because
their state is in the database. If a process crashes repeatedly, read the log before restarting it in a
loop.

Logs go to the console and to a daily rotating file; see [logging.md](logging.md). Python's logging writes
to stderr, so some platforms tag every line as an error: the level inside the text (`INFO`, `WARNING`, ...)
is the real one.
