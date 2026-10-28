# Contributing

Thanks for taking an interest in Keep-Dreaming-Bot. It is a hobby project made for one Discord server, so
changes are reviewed with that in mind: small, tested and in keeping with the existing code.

## Before you start

- **Open an issue first for anything bigger than a small fix.** New features and changes to ratings or
  matchmaking are discussed and agreed before they are built, because they change what the players see.
- Bugs: say what you ran, what you expected, what happened, and the relevant lines of `logs/kdbot.log`
  (remove tokens and IDs you do not want to share).
- Security problems go through [SECURITY.md](SECURITY.md), not a public issue.

## Set up

```bash
git clone https://github.com/PM-Delgado/Keep-Dreaming-Bot.git
cd Keep-Dreaming-Bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env               # a bot token and a test server are only needed to run the bot
```

The tests need no token and no network. To try the bot itself, set `ENABLE_MOCK_PLAYERS=true` and follow
[docs/testing.md](docs/testing.md).

## Before you open a pull request

```bash
ruff check . && ruff format --check .
pyright
pytest                 # add -m "not slow" to skip the long simulations
```

All three must pass. Add or update tests for changed behaviour; business logic is tested without Discord
(use mock interactions where needed). If you change a command, its options or its output, update
[docs/commands.md](docs/commands.md) and the README in the same pull request.

## How the code is organised

- A feature lives in `bot/features/<name>/` and is registered by adding its `cog` module to `EXTENSIONS`
  in `bot/app.py`.
- `service.py` holds the rules and imports nothing from Discord (the clock and RNG are injectable),
  `repository.py` is SQL only, and `cog.py` is thin: commands, embeds and tasks.
- Settings are read only in `bot/config.py` and passed in; there is no `os.getenv` elsewhere.
- Schema changes append a script to `MIGRATIONS` in `bot/db.py`. Never edit a migration that has been
  applied.
- Writes that must stand or fall together go inside `async with db.transaction():`. Keep the body to
  database work.
- Slash commands only, with default intents (no privileged intents).
- Everything the players read is in Portuguese; code, comments, docs and commit messages are in English.

## Style

- Type hints and clear names; match the surrounding code (ruff and pyright enforce the rest).
- Small, reviewable changes scoped to one task; no unrelated refactors or speculative abstractions.
- No new dependency without a concrete need; check the standard library and the current dependencies
  first.
- Never commit tokens, API keys, `.env` files, databases or real Discord IDs. `.env`, `data/` and `logs/`
  are git-ignored for that reason.
- Handle Discord API failures, invalid input and missing permissions gracefully: a refused command
  answers the user privately and is logged, it does not raise.

## Riot data

The Riot integration is optional and handles other people's account data. Read
[docs/riot-data.md](docs/riot-data.md) before changing what is requested or stored, and keep the
disclaimer in the README.
