# Testing the bot by hand

There are three ways to try the bot without a full group: **mock players** (below), a **roster with test
games** and the **simulator** (no Discord). The first sections cover lobbies with mock players; the checklist
further down says what every command should answer.

## Mock players

You do not need ten friends to try the lobby: **mock players** are fake participants that live in
the database (flagged `is_mock`, with negative IDs that can never match a real Discord user) and
are driven by admin commands. Everything else, the buttons, the ready-check, the balancer and the
messages, is the real code.

**Players never choose roles.** Roles come only from each player's profile (admin override, what
the player declared, or the Riot estimate). The balancer puts everyone on the role that suits them
best, or their next best when roles collide, and it pairs strong players against strong players in
the same lane. There is no role menu and no way to ask for a different split.

### One-time setup

In `.env` (git-ignored):

```
ADMIN_USER_IDS=<your Discord user id>
ENABLE_MOCK_PLAYERS=true
READY_CHECK_SECONDS=30        # shorter than the default 120, so timeouts are quick to test
```

Restart the bot. You should see a log line saying mock players are enabled. **Turn
`ENABLE_MOCK_PLAYERS` off (and run `/mock clear`) before real use.** Mock players are never pinged
and are marked with 🤖 in lobby messages. You must be registered yourself: `/register your-name`.

### The quick path: a full game alone

1. `/lobby open` in a channel. You are player 1.
2. `/mock fill` (default: 9 mock players). The 10th player starts the **ready-check**; you get a
   ping with a countdown. Mocks have random League ranks and one random main role each.
3. `/mock ready` confirms every mock. Then press **Pronto** yourself.
4. Teams appear: Azul / Vermelho with a role for each player and a coarse balance label
   ("Equilibrado", "Ligeira vantagem ...", ...). No ratings are shown. A note like `· 4/5` after a
   name means that player was **not** put on their best role and shows their level on the role they
   got; no note means they are on their best.
5. **Confirmar** starts the game (creator or admin only). Report the result with the **Azul ganhou** /
   **Vermelho ganhou** buttons or `/mock report`; a started game nobody reports is voided after
   12 h, or use **Cancelar**. Afterwards try `/match stats`, `/history`, `/stats` and `/leaderboard`.

### Testing the role logic

`/mock list` shows each fake player's best roles and rank, so you can check the teams against it.
`/mock create count:5 main_role:Jungle` makes five players who all main the same role.

| Scenario | How | Expected |
|---|---|---|
| Five junglers | `/mock clear`, `/mock create count:5 main_role:Jungle`, `/mock create count:4`, `/lobby open`, `/mock fill`, `/mock ready`, **Pronto** | Exactly two junglers play Jungle, one per team. The other junglers are on their next-best role and carry a `· 4/5`-style note |
| Everyone mains the same role | `/mock create count:9 main_role:Mid` (after `/mock clear`), then fill and ready | Two Mids (one per team); the others go to their next-best role, flagged with notes |
| Equal strength, same lane | Set up a lobby where the two highest-ranked players share a main role (`/mock list` helps) | They end up on opposite teams in the same role (or both off it), not one crushing a weak lane |
| Stars spread out | Look at the teams for the highest-ranked mocks | The strongest players are not all on one team |
| Cancel and retry | **Cancelar**, then open a new lobby | Only way to get a different split; there is no reroll |

### Other scenarios

| Scenario | How | Expected |
|---|---|---|
| Not everyone responds | After `/mock fill`, do **not** run `/mock ready`; wait for the countdown | Silent players are dropped; reserves (if any) move up and the check restarts; otherwise the lobby goes back to "Aberta" |
| Someone declines | Press **Recusar** during the ready-check | You leave; a reserve takes your place with a fresh round, else back to "Aberta" |
| Reserves | `/mock fill count:11` (you + 10 mocks) | Ten play, one is listed under "Reservas" |
| Players leave | `/mock drop` | All mocks leave; the lobby returns to "Aberta" |
| Double clicks | Press a button twice fast | Only one action happens; the second gets a short message |
| Permissions | A non-admin, non-creator account presses **Confirmar**/**Cancelar** | Refused with an explanation |
| Restart | Restart the bot mid-lobby, then press a button on the old message | It still works (state is in the database; buttons are persistent) |
| Lost message | `/lobby show` | The lobby is re-posted and the old copy removed |
| Result flow | After **Confirmar** (teams): `/mock report winner:Azul`, then press **Confirmar resultado** or **Contestar** | With only your account (an admin) the confirm is final at once; use `/mock confirm` to confirm as a mock on the other team |
| Auto-confirm | `/mock report`, then wait 30 minutes | Confirmed automatically, with a summary in the channel; **Contestar** stops it |
| Role and champion icons | Start the bot once with a real token, then open a lobby and play it through, `/match show`, `/profile view`, `/riot history` | Role icons replace the role names (lobby teams, `/match show`) and champion icons replace champion names; `/match show` lines read `<role> Name — <champion> 8/2/11`. With `CHAMPION_ICONS=false` or before the upload finishes, names are shown instead |
| Champion and KDA | After a confirmed game: `/match stats champion:<type to search> kills deaths assists`, then `/match show`, `/match pending`, `/match validate match:<n>` | Autocomplete lists champions; the game shows `· Ahri 8/2/11 ⏳` until validated (then no mark) and its footer says `Resultado confirmado por X · KDAs validados por Y`. `/mock stats match:<n> validate:True` fills the mocks' stats |
| Placement | Finish games with the same players | *Colocação n/5* in the summary; the tiers appear after the 5th confirmed game |
| Correction | `/match fix match:<n> winner:...` or `/match void match:<n>` | The message and ratings follow; `/match show` shows the new result |
| Scheduled lobby | `/lobby schedule when:<a few minutes ahead> note:teste`, then `/mock fill lobby:<n>` | Message titled "Agendada" with the time; ten sign-ups wait; at the time the ready-check starts |
| Reminder | Schedule 31 min ahead and wait one minute | A reminder pinging real signed-up players (never mocks); sent once, also after a restart |
| Too few at the time | Schedule, sign up fewer than ten, wait for the time | A one-off "só há N/10" notice; the tenth sign-up afterwards starts the ready-check at once |
| Busy player | Be in a running game and let a scheduled lobby you signed up for reach its time | You are dropped from the scheduled one |
| Several lobbies | `/lobby open`, `/lobby schedule`, then `/lobby list` | Both are listed; `/lobby show lobby:<n>` and `/lobby cancel lobby:<n>` pick one |

To change a player's roles for a test, use `/profile roles` (yourself) or `/admin roles set`.

## With a roster and test games

Seed a roster file and play test games with its players, then start the bot on that database (see
[the roster tool](roster-tool.md)). This is the way to review every embed with realistic data:

```bash
python -m bot.features.roster seed
python -m bot.features.roster games
python -m bot
```

You get every player of the file with their rank, roles and Riot accounts, and games in every state:
confirmed by players, by an admin and automatically, corrected, voided before and after the result,
contested, reported and in progress. Stats are validated, pending or missing; one of your games has a
played role that differs from the assigned one; two players are still in placement. The reported game
is auto-confirmed about 20 minutes after it is played, so look at it first. A database cannot hold the
*lobby message* of an open or scheduled lobby: use `/lobby open`, `/lobby schedule` and `/mock` for
those. Games in progress block their players from joining a lobby: finish or `/match void` them first.
When you are done, `python -m bot.features.roster reset --keep-roster --yes` removes the games (and
`reset --yes` followed by `seed` rebuilds the roster from scratch).

## Checklist: what each command should answer

Texts are in Portuguese, as the bot shows them.

| Try | Expect |
|---|---|
| `/register Ana` | Private: "🎉 Bem-vindo aos Scrims, **Ana**! Define as tuas roles com `/profile roles` e o teu rank com `/profile rank`." A second `/register`, or a nickname already taken: a private error. |
| `/profile roles role:Mid proficiency:5` | Private: "✅ <Mid icon>: 5/5." (`Limpar`: "🧹 <icon>: limpa."). |
| `/profile rank tier:Gold division:II` | Private: "✅ Rank de referência: **Gold II**. Serve só como ponto de partida; ..." |
| `/profile view` | Public embed: the nickname as a large heading with the Discord avatar, the side bar coloured by the League Solo/Duo tier (blurple without a rank); *Scrims* (`Rank:` tier, division and a progress bar toward the next division, full on the top step, or "Sem rank (colocação n/5)"; record, win rate, `KDA: 4.84 (6.4/3.7/11.5)`), *Win Rate Scrims* (`icon 2V/0D` for all five roles, `0V/0D` where there are no games), *Campeões Scrims* (`role champion 2V 0D · KDA`), *Roles* (icon and `n/5`, the name only where there is no icon), then *Rank League*, *Contas Riot* and *Campeões Solo/Duo* (icons only) when there is data; no footer. Not registered: private "KD Scrims: Jogador não registado. Usa /register primeiro." |
| `/lobby open` | Public lobby message "🟢 Scrim #n · Aberta" (large heading, bullets saying what happens next, places bar, text-only buttons). A second open: a private error. |
| `/mock fill`, `/mock ready`, then **Pronto** | Ready-check ping, then balanced teams (Azul/Vermelho) with a role icon per player and no ratings shown. |
| **Confirmar**, then `/mock report winner:Azul` | "Resultado por confirmar"; **Confirmar resultado** / **Contestar** appear for the other team. As an admin your confirm is final at once; otherwise use `/mock confirm`. |
| After the result | Public summary: "🏆 Scrim #n: **Azul ganhou**!", 🎯 finished placement / 📈 / 📉 lines, "Em colocação: ..." and the `/match stats` hint. |
| `/match stats champion:Ahri kills:8 deaths:2 assists:11` | Private: "📝 Jogo #n: **Ahri 8/2/11** registado. Falta um admin validar ⏳" |
| `/match show` | Public embed: teams with `<role icon> Name — <champion icon> 8/2/11`, ⏳ while pending, bottom line "Resultado confirmado por X · KDAs validados por Y · N por validar". |
| `/match pending`, then `/match validate match:n` | Private list "⏳ Stats por validar" ("Tudo em dia! 🎉" when empty); then "✅ Jogo #n: k stats validadas." |
| `/match set match:n member:@x champion:Zed kills:1 deaths:2 assists:3 role:Top` | Private confirmation; the stats are validated at once and the played role shows. |
| `/match fix match:n winner:Vermelho`, `/match void match:n reason:teste` | Private "✏️ Jogo #n: **Vermelho** ganhou." / "🚫 Jogo #n anulado." plus a channel summary ("✏️ Resultado ... corrigido" / "🚫 Scrim #n anulado (teste)"); ratings follow. |
| `/history`, `/stats`, `/leaderboard board:...` | The public embeds described in the Players table (no footer on any of them). A brand-new player: "Ainda não há jogos confirmados."; boards without enough games: "Ainda ninguém tem 5 jogos." / "Ainda ninguém terminou a colocação." |
| `/lobby schedule when:amanhã 21:00 note:teste`, `/lobby list`, `/lobby cancel` | Public "📅 Scrim #n · Agendado" message; the list shows both lobbies; cancel answers privately "🛑 Lobby cancelada." |
| `/riot link riot_id:Nome#TAG` (needs the key) | Private summary (plus the consent notice when linking your own account); `/riot list` and `/riot history` answer publicly. |
| `/admin rating inspect user:@x` | Private embed with H, R and E (raw numbers; the only place they appear). |
| `/admin rating set`, `/admin rating rank`, `/admin roles set`, `/admin rating recompute` | Private one-line confirmations; "🔄 Ratings reconstruídos." for recompute. |
| `/sync` | Private: "🔄 Sincronizados n comandos." (bot owner only; anyone else is refused). |
| A non-admin running an admin command | Private: "⛔ Não tens permissão para usar este comando." An unexpected failure: "💥 Ocorreu um erro inesperado. Tenta novamente mais tarde." |
| `/puta`, `/putaindor` | Public name-of-the-day embeds ("Ainda não há ranking; usa /puta para começar." before any pick). |

## Cleaning up

* `/mock clear` deletes the fake players and everything tied to them (ratings, role levels, lobby
  entries), **and the finished test games they played**: your own rating is rebuilt without them. It refuses while any of them is in an unfinished lobby, so cancel lobbies first. Real
  players are never touched.
* Do not leave mock players in a database you care about.

## Matchmaking without Discord

`python -m bot.features.simulation season --players 14 --games 40 --seed 2` plays a whole season through the real lobby, match and rating code (no Discord) and prints each player's results, visible rank and whether the invariants hold (stored ratings equal a full replay, nobody has a tier before 5 games, nobody is left busy). `python -m bot.features.simulation scenario all` runs the balancer on fixed rosters (five junglers, two Masters who both play mid, a Master among Golds, ...) and checks the expectations; `population --style realistic --lobbies 200` and `league --players 20 --lobbies 150` run many random lobbies. Use `pytest -m "not slow"` to skip the long simulations.

## What these tests do not cover

Several real people pressing buttons at the same moment, Discord rate limits on message edits with
many players, and how the messages look on mobile. The automated tests cover the lobby rules,
races (simultaneous joins, readies, confirms and deadlines), the balancer (including the junglers
and Masters scenarios) and the rendering, without Discord.
