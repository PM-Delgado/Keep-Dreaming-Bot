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
| `/riot history [player] [riot_id]` | The last 10 ranked Solo/Duo games of a player (all their accounts, or one): result, champion, role, KDA, CS, duration, age. Reads what the last sync stored (no Riot requests), visible to the group. Formatting lives in `bot/features/riot/history.py` and `build_history_embed`. |
| `/riot main`, `/riot unlink`, `/riot refresh`, `/riot list` | Change the main account, delete an account and all data tied to it (if it was the main, the earliest-linked remaining account becomes the main), refresh names/rank/ranked history (10 min cooldown for players), list the linked accounts (visible to everyone). |
| `/lobby schedule when [note]`, `/lobby list` | Schedule a lobby for later (Lisbon time: `21:00`, `amanhã 21h30`, `sexta 21:00`, `15/08 21:00`). People sign up in advance; 30 minutes before, the signed-up players are pinged; at the time, ten sign-ups start the ready-check (fewer: it starts as soon as the tenth joins). Several scheduled lobbies can exist at once; signing up does not block you from playing another game before. `/lobby list` shows the open and scheduled lobbies; `/lobby show lobby:<n>` and `/lobby cancel lobby:<n>` take a lobby number. |
| `/lobby open`, `/lobby show`, `/lobby cancel` | Open the in-house lobby in this channel (one open lobby per server). A live message with buttons: **Entrar / Sair / Pronto / Recusar / Confirmar / Cancelar**. At 10 players a ready-check (default 2 min) starts; non-responders are replaced from the reserves; when all ten confirm, balanced teams are proposed (coarse balance label only). Players never choose roles: each is placed from their profile proficiency (best role, or next best when roles collide), strong players are matched against strong players in the same lane, and there is no reroll. Only the creator or an admin confirms the teams or cancels. State is in SQLite and the buttons survive restarts. `/lobby show` re-posts a lost message. |
| Results (buttons on the lobby message) | After the teams are confirmed the game is recorded. Someone who played presses **Azul ganhou / Vermelho ganhou**; someone on the *other* team presses **Confirmar resultado** (or **Contestar**). With no objection the result is confirmed automatically after 30 minutes; a contested one waits for an admin (winner buttons, or `/match fix`). An admin's report is final at once. Only confirmed results change ratings; players are reminded after 90 minutes without a result and an unreported game is voided after 12 h. When a result is confirmed the channel gets a summary with promotions, demotions and placement progress (never raw ratings): nobody has a visible tier before their 5th confirmed game (*Colocação n/5*). |
| `/match show [match]`, `/match void match [reason]`, `/match fix match winner` | `show` lists a game's status, teams and history. Admin only: `void` cancels a game (undoing its effect on ratings if it was confirmed), `fix` corrects a confirmed result or decides a pending one. Every action is kept in an audit trail. |
| `/mock create|fill|ready|drop|report|confirm|list|clear` | Admin-only (`fill`, `ready` and `drop` take an optional `lobby` number for scheduled lobbies), and only with `ENABLE_MOCK_PLAYERS=true`: fake players to test lobbies alone. See `docs/testing-lobbies.md`. |
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
    simulation/ mock-player scenarios, random populations and a rating league through the real balancer
                (`python -m bot.features.simulation list|scenario|population|league`); never loaded by the bot
    balance/    pure team balancer (126 splits; role-dependent strength, lane matchups, star spread); no Discord/DB
    lobby/      state machine + SQL (service.py, repository.py), render.py (messages), views.py (buttons),
                cog.py (commands, sweeper), mock.py (test players)
    ratings/    pure rating core (no Discord): rank.py R, matchmaking.py E, ladder.py, engine.py
                (the only openskill import), replay.py; service.py / repository.py
tests/
tools/          dev-only helpers (ladder_sim.py: inspect tier and drift distributions)
```

New features go in `bot/features/<name>/` with a `cog.py` exposing `setup(bot)`, and are added to `EXTENSIONS` in `bot/app.py`.

## Data

SQLite file at `DB_PATH` (default `data/kdbot.sqlite3`, git-ignored). Tables: `names`, `phrases`, `picks` (one row per day); Lobbies (migrations 5-7): `lobbies`, `lobby_entries`; Matches (migration 8): `matches`, `match_participants`, `match_audit`; Riot (optional, migrations 3-4): `riot_accounts`, `riot_role_games`; in-house: `players`, `player_roles`, `rating_events` and `rank_signals` (authoritative inputs) and `ratings`, `rating_history` (caches rebuilt by replay). On first run, names and phrases are loaded from `bot/features/nameday/seed/*.json`. Schema changes are versioned in `bot/db.py` (`MIGRATIONS`). To back up, copy the `data/` folder while the bot is stopped. To reset the ranking, stop the bot and delete the `.sqlite3` file (and `-wal`/`-shm` files).

## Local setup

1. Python 3.11+. `python -m venv .venv`, then `.venv\Scripts\activate` (Windows).
2. `pip install -e ".[dev]"`
3. Copy `.env.example` to `.env`; fill in `DISCORD_TOKEN` (Bot tab -> Reset Token) and `GUILD_ID`.
   Optionally set `RIOT_API_KEY` (and `RIOT_DEFAULT_PLATFORM`, default `euw1`) to enable `/riot`; see `docs/riot-policy.md` first.
   Lobby options: `READY_CHECK_SECONDS` (default 120) and `ENABLE_MOCK_PLAYERS` (testing only).
   Optionally set `ADMIN_USER_IDS` (comma-separated Discord user IDs) to enable the admin commands; empty means nobody.
4. Invite the bot with scopes `bot` + `applications.commands`. No privileged intents are needed.
5. Run from the repo root: `python -m bot`

## In-house ratings in one paragraph

Three quantities per player: **R** (rank-based estimate from the main League account's Solo/Duo rank, or a self-reported/admin rank), **H** (hidden in-house rating, OpenSkill, seeded from R and changed only by confirmed in-house results) and **E** (the matchmaking estimate, `R + D*tanh((H-R)/D)`, so E can never drift more than `d_down`/`d_up` from R). H is replayed from `rating_events` plus confirmed matches, so every cache can be rebuilt (`/admin rating recompute`); E and the visible ladder are computed from stored inputs, so tuning `MatchmakingParams` or `LadderConfig` never rewrites history. Run `python tools/ladder_sim.py --help` to compare parameter sets.

## Checks

```
ruff check . && ruff format --check .
pyright
pytest                 # -m "not slow" skips the population/league simulations (~30 s faster)
```

Matchmaking can be checked without Discord: `python -m bot.features.simulation season --players 14 --games 40` plays a whole season through the real lobby, match and rating services (in memory) and prints who ranked up and whether every invariant holds; and `python -m bot.features.simulation scenario all` prints how the balancer lands each scenario (five junglers, two Masters in mid, a Master among Golds, ...); `population` and `league` run many random lobbies. `tune` is the tuning lab: it plays many seasons of a realistic 20-player group (Master to Bronze, true skill deviating from the Riot rank, hidden form) under different settings, in parallel and with the same seeds for every setting, and compares estimate error, balance and tier stability: `tune baseline`, `tune sweep sigma_rank_seed 2 3 4.5`, `tune compare "a:d_up=3" "b:beta=8"`, `tune settings` (see `docs/tuning.md`). The same harness is where match-result, MMR and rank-gain checks will go.
