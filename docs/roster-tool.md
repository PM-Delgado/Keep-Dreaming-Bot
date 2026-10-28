# The roster tool

A roster file lets you register, rank and link a whole group of players before the bot goes live, instead of
each person running `/register` first. The tool is `python -m bot.features.roster` (stop the bot first; every command that
writes makes a backup copy next to the database, `*.sqlite3.bak-<time>-<reason>`).

1. **Fill the file.** Copy `tools/roster.example.json` to `data/roster.json` (git-ignored: it holds real
   Discord ids and Riot names) and fill one entry per player: `discord_id`, `nickname` (2-24
   characters, unique), `rank` (tier and division, or `null`), `roles` (role to proficiency 1-5),
   `riot` (a list of `Name#TAG`, the first is the main account), `platform` and, optionally,
   `strength`. The file explains each field at the top.
2. **Check it.** `python -m bot.features.roster check` reads the file, lists every problem at once
   (wrong tier, duplicate id, bad Riot ID...) and shows each player with the strength a simulation
   would use. It changes nothing.
3. **Seed the database.** `python -m bot.features.roster seed` registers everyone through the same
   services as `/register`, `/profile rank` and `/profile roles`, and links the Riot accounts through
   the real `/riot link` process (needs `RIOT_API_KEY`; with `--no-riot` they are skipped). It can be
   run again: players already there are kept, only a changed rank or role is applied, and accounts
   already linked are skipped. The `/puta` names, phrases and history are never touched.
   It prints each player as it goes and finishes in a couple of minutes. The ranked-history download
   (up to 100 games per account, about one request every two seconds on a personal key) is *not*
   done here: the bot continues it in the background when it starts. `--wait-history` does it now
   (expect an hour). Press Ctrl+C at any time; running `seed` again continues. The first line of every
   command shows which database it uses and where that came from. If it says `DB_PATH environment
   variable` and you did not mean it, clear it (`Remove-Item Env:DB_PATH` in PowerShell).
4. **Play test games with the roster's players** (optional, for reviewing the bot's messages):
   `python -m bot.features.roster games` plays 22 finished games through the real lobby, balancer and
   rating services, plus a contested, a reported and an in-progress game. Who wins follows each
   player's hidden `strength` (the file's value, or one derived from the rank), so you can tweak it
   for people you know are over- or under-ranked. Two newcomers stay in placement. The three
   unfinished games are completed with mock players (`Mock Extra NN`), because nobody can be in two
   games at once. Options: `--games`, `--newcomers`, `--seed`, `--guild-id`.
5. **Judge the matchmaking.** `python -m bot.features.roster matches` lists every game in order,
   lane by lane: blue against red, each player's matchmaking estimate (E, in rating points), the gap per
   lane, the balancer's expected win chance, the winner, and whether the favourite or the underdog
   won. It ends with a summary (how many games were near even, how often the favourite won, the
   average lane gap, the most lopsided game). `--match 12` shows one game, `--finished` skips
   voided and unfinished ones. It only reads the file, so it is safe while the bot runs. In Discord,
   `/match show match:12` posts one game's teams, but never the numbers.
6. **Go back to a clean database.** `python -m bot.features.roster reset` shows what would be removed
   and changes nothing; with `--yes` it deletes **all league data**: players, roles, ranks, Riot
   accounts and their stored games, ratings, lobbies, games and mock players. Run `seed` afterwards
   to put the roster back. The `/puta` names, phrases and picks, the champion list and the uploaded
   icon ids always stay. `reset --keep-roster --yes` is the lighter version: it removes only the test
   games, lobbies, rating adjustments and mock players and keeps the roster (use it to replay `games`).

7. **Prepare the database for production.** `python -m bot.features.roster production` shows the
   plan and changes nothing; with `--yes` it makes a backup and then, in one go: deletes all league
   data (test games, lobbies, mock players, rating adjustments), **clears the daily `/puta` picks**,
   seeds the roster again (links the Riot accounts, so it needs `RIOT_API_KEY`; `--no-riot` skips
   them) and checks the result table by table. The `/puta` names and phrases are kept exactly as
   they are (on an empty database they come from `bot/features/nameday/seed/*.json`), and so are
   the champion list and icon ids. The check ends with `Ready.` or the `FAIL` lines to fix: integrity,
   foreign keys, names and phrases present, no picks, every player of the file registered with a
   rating and rank, no mock players, no games or lobbies, and the Riot accounts of the file linked.
   Do not run `games` afterwards.

The roster's own rows are dated just before the first test game (so the history can sit in the past);
`games` refuses to run on a database that already has games, run `reset --keep-roster --yes` first. To pick a
player in `/profile view`, `/history` or `/stats`, that person must be a member of the server you
test in; everyone else still shows up in `/leaderboard`, `/match show` and the games.
