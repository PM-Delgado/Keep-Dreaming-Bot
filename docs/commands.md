# Commands

All commands are slash commands, registered only in the configured server. *Reply* says whether the
answer is **public** (everyone in the channel sees it) or **private** (only the person who ran it;
every error message is private). Commands marked *admin* need your Discord user ID in
`ADMIN_USER_IDS`. Parameters in `[brackets]` are optional.

#### Players

| Command | Reply | What it does |
|---|---|---|
| `/register <nickname>` | private | Join the Scrims roster (2-24 characters, unique). Starts at a default rating until you give a rank. |
| `/profile view [user]` | public | Nickname (large) with the Discord avatar and a side bar in the colour of the player's League Solo/Duo tier; *Scrims* (`Rank:` with a progress bar or placement `n/5`, record and win rate, `KDA:` over all validated games); *Win Rate Scrims* (icon and `2V/0D` for all five roles, `0V/0D` where there are no games); *Campeões Scrims* (top 5 role + champion, validated stats); *Roles* (icon and proficiency); and last the League reference: *Rank League*, *Contas Riot* (★ = main) and *Campeões Solo/Duo* (icons of the top 3 of the stored ranked games, the games of all the player's linked accounts added up). If the player's Riot data is older than 30 minutes the bot first asks Riot (up to 4 seconds; the answer then arrives as a reply to "thinking..."), so rank and Riot IDs are current without running `/riot refresh`; a Riot failure never blocks the profile. No footer. A player who is not registered gets a private error. |
| `/profile roles <role> <proficiency>` | private | Set how comfortable you are (1-5) on a role, or `Limpar` to clear it. |
| `/profile rank <tier> [division]` | private | Report your League Solo/Duo rank; it seeds your starting rating while you have few games. |
| `/history [user]` | public | Large heading `Histórico · Name` with the Discord avatar and a side bar in the player's League tier colour; `Jogos: 10V 8D (56%) · 18 jogos`, then the last 10 confirmed games: result, role, champion and KDA (validated stats only; pending ones show "stats por validar"). No footer. Not registered: the same private error as `/profile view`. |
| `/stats [user]` | public | Same look as `/profile view` and `/history` (large heading, avatar, tier-coloured side bar, no footer): `Jogos:` and `KDA:` lines, *Por role* (all five roles, `0V 0D` where there are no games), *Campeões Scrims* (top 5) and *Campeões Scrims por role* (role, champion; validated stats only). Not registered: the same private error as `/profile view`. |
| `/leaderboard [board]` | public | `board` is *Rank Scrims* (default: players past placement, by tier and division with a progress bar toward the next division; inside a division the fuller bar ranks higher; KD esports, the top step, shows a full bar), *Win Rate Scrims* or *KDA Scrims* (both need 5 games; KDA counts validated games; equal scores share a position, A-Z). Same look as the profile: large heading with an emoji, a one-line rule (`n jogadores · mín. 5 jogos`), 🥇🥈🥉 on the podium, the leader's Discord avatar as the picture, a colour per board (gold, green, red), no footer. Ratings are never shown as numbers. |

#### Lobbies and games

| Command | Reply | What it does |
|---|---|---|
| `/lobby open` | public | Open the server's Scrim lobby in this channel (a live message with buttons and a progress bar of the 10 places). With `SCRIM_CHANNEL_ID` set it only works in that channel (elsewhere: a private error naming it), and with `SCRIM_ROLE_ID` set the message starts with the role tagged inside a spoiler, which still notifies it. |
| `/lobby schedule <when> [note]` | public | Schedule a lobby (Lisbon time, e.g. `21:00`, `21h30`, `amanhã 21:00`, `sexta 21:00`, `15/08 21:00`, `2026-08-15 21:00`). A reminder goes out 30 minutes before. Same channel limit and spoiler-tagged role as `/lobby open`. |
| `/lobby list` | public | Heading `📋 Scrims`, then the open lobby and the scheduled ones (status emoji each); a friendly message when there are none. |
| `/lobby show [lobby]` | public | Re-post a lobby message here (the stale copy is removed; no new tag). Same channel limit. |
| `/lobby cancel [lobby]` | private | Cancel a lobby (its creator or an admin). |
| `/match show [match]` | public | Large heading `🏆 Jogo #n · Confirmado`, the winner, teams (role icon, name, champion icon, KDA, ⏳ = pending); a confirmed game takes the winning side's colour; a small line at the bottom says who confirmed the result and who validated the KDAs. Default: the latest game. |
| `/match stats <champion> <kills> <deaths> <assists> [match]` | private | Record your own champion (autocomplete) and KDA (0-99) for a confirmed game; default: your latest game without validated stats. Waits for an admin to validate it. |
| `/match void <match> [reason]` | private + channel notice | *Admin.* Cancel a game; undoes its effect on ratings if it was confirmed. |
| `/match fix <match> <winner>` | private + channel notice | *Admin.* Correct a confirmed result or decide a pending or contested one. |
| `/match set <match> <member> [champion] [kills] [deaths] [assists] [role]` | private | *Admin.* Set or correct someone's stats (validated at once) or the role they actually played. |
| `/match validate <match> [member]` | private | *Admin.* Validate the pending stats of a game (of one player, or of everybody). |
| `/match pending` | private | *Admin.* List the stats waiting for validation, oldest game first. |

The lobby message carries the buttons (each with an emoji): **Entrar**, **Sair**, **Pronto**, **Recusar**, **Confirmar**
(teams), **Azul ganhou** / **Vermelho ganhou**, **Confirmar resultado**, **Contestar** and
**Cancelar**. Only the buttons that make sense for the current state are shown. When a result is
confirmed, corrected or voided the bot also posts a summary in the channel (winner, promotions,
demotions, finished placements, and a reminder to use `/match stats`).

#### Riot accounts (optional, needs `RIOT_API_KEY`)

Without the key every `/riot` command answers that the integration is disabled.

**Automatic posts** (need `RIOT_API_KEY` and `RIOT_FEED_CHANNEL_ID`; no command): every 5 minutes the bot checks **every linked account** (of real players still in the group, not only the main) for a new ranked Solo/Duo game and posts its card in the feed channel, the same card `/riot card` previews, now with the LP gained or lost in the title (`Ganhou 27 LP (Solo/Duo)`) and the rank after the game. The LP is the new league points minus the account's last known ones (promotions and demotions count across divisions, Master and above on one scale). It is **left out rather than guessed** when it cannot be told: several games finished since the last look (their sum would be wrong, so none is shown and only the newest card shows a rank), or Riot has not updated the rank yet (the bot waits up to 10 minutes, then posts without a number). Remakes and games that ended more than 2 hours ago are not posted. The first time the bot sees an account it only remembers its rank: games from before that are never posted. A card that Discord refuses is tried again at the next check. Nobody is pinged. The post carries the player's avatar and `Nickname (Name#TAG)`.

| Command | Reply | What it does |
|---|---|---|
| `/riot link <riot_id> [platform] [player]` | private | Link a Riot account (`Name#TAG`; the platform defaults to `RIOT_DEFAULT_PLATFORM`). The first becomes the main. Linking your own account shows the consent notice; an admin linking for someone gets only the private summary (nothing is posted in the channel and the player is not pinged: the consent is agreed outside the bot). Only admins may use `player`. |
| `/riot main <riot_id> [player]` | private | Choose which account supplies the rank. |
| `/riot unlink <riot_id> [player]` | private | Remove an account and all data tied to it. |
| `/riot list [player]` | public | Large heading `Contas Riot · Name` with the avatar and the tier-coloured side bar; one line per account (★ = main, games stored, last sync); no footer. |
| `/riot leaderboard [post]` | public | Solo/Duo ranks of every player's **main** account, highest LP first. Every player takes exactly two lines, so a long name or a trend can never make one row taller than the others: `🥇 **Nickname** · 🟣 MASTER · 1034 LP` and, in small text underneath, `Name#TAG · 📈 +27` (medals for the top 3, the position number after that; the icon is the tier's ranked emblem, or a generic colour dot if it is not uploaded; players without a rank last as `Sem rank`). `📈`/`📉` = LP since the previous day's snapshot, shown only when it changed. On the first page, when anybody moved, a line names the biggest climb and fall (`🔥 Maior subida` / `🧊 Maior queda`); the footer explains the arrows only then. The side bar has the colour of the leader's tier. Ten per page with persistent ◀ ▶ buttons that edit the message in place (they wrap around). Players who left and mock players are not listed. If any main's rank is missing or older than an hour the bot first asks Riot (up to 10 seconds; the answer then arrives as a reply to "thinking..."). `post:true` (admins only) refreshes the ranks and also posts it in the `RIOT_FEED_CHANNEL_ID` channel without recording a trend snapshot. The same message is posted there automatically every day at 10:00 Lisbon time, and that one records the snapshot. |
| `/riot card [player] [riot_id] [post]` | admins only, private | **Temporary: a preview of the card the bot posts automatically (see above); remove once it is no longer useful.** Fetches the player's newest stored ranked game again from Riot (the match, its 15:00 timeline and the account's current rank: up to 3 requests, nothing stored) and shows the card privately. The card: the player's avatar and `Nickname (Name#TAG)`, a title `Ganhou/Perdeu N LP (Solo/Duo)` (here without the number: the LP change is only known for games the feed saw live), the champion as thumbnail, role icon and champion, the rank, `🔥 Quadra kill` for the best multikill, then bold-named fields **KDA**, **Duração**, **Participação** (kill share), **CS**, **Dano** (with the share of the team's damage), **Visão**, **Gold**, **Pings** (blank fields pad the last row so the three columns line up), then a divider line and a wide **Stats @ 15 (vs [icon] Zed)** block (the opponent's champion icon, as a small emoji, before its name): `CS -1 | Gold +146 | XP +380 | Nível 2 primeiro ❌`, i.e. the difference to the lane opponent (every role) and whether the player reached level 2 first (✅ or ❌), labels in bold, `|` between them and no colons. The divider is a line of box-drawing characters; Discord has no real divider or fixed card width, so it also sets the card's width. The side bar is green, red or grey (remake); a stat Riot did not send is left out. Footer: `Solo/Duo` and the game's time (no match id). `post:true` also posts it in the `RIOT_FEED_CHANNEL_ID` channel. |
| `/riot refresh [player]` | private | Refresh names, rank and ranked history right now (10 minute cooldown for players; admins skip it). Rarely needed: profile views, ready-checks and a 6-hourly timer already refresh automatically (see [docs/riot-data.md](riot-data.md)). |
| `/riot history [player] [riot_id]` | public | Heading `Solo/Duo · Name` with the avatar and tier colour, `Jogos: 6V 4D (60%) · KDA: 3.25`, then the last ten ranked Solo/Duo games (one line each: result, champion and role icons, KDA, CS, duration, and `+27 LP` / `-19 LP` for games the automatic feed saw live (older games have none); a ★ in front of the games played on the main account, no account names); a small line says when it was last updated. If an account's history is older than 5 minutes the bot first asks Riot for new games (up to 4 seconds; the answer then arrives as a reply to "thinking..."); a Riot failure never blocks it. |

#### Admin

All replies are private.

| Command | What it does |
|---|---|
| `/admin player register <member> [display_name]` | Register a server member in advance. An existing player is never modified. |
| `/admin rating inspect <user>` | Show the hidden rating (H), rank estimate (R) and matchmaking estimate (E): the only place raw numbers appear. |
| `/admin rating set <user> <mu> [sigma] [note]` | Adjust a player's hidden rating (mu 0-100, sigma 0.5-25). Placement still applies. |
| `/admin rating rank <user> [tier] [division]` | Override a player's reference rank (leave the tier empty to clear the override). |
| `/admin rating recompute` | Rebuild all ratings from the stored games. |
| `/admin roles set <user> <role> <proficiency>` | Override a role proficiency (`Limpar` clears it). |
| `/sync` | *Bot owner only.* Re-sync the commands (also done on startup). |

#### Testing (only with `ENABLE_MOCK_PLAYERS=true`, admin, private replies)

| Command | What it does |
|---|---|
| `/mock create [count] [main_role]` | Create 1-30 fake players, optionally all with the same main role. |
| `/mock fill [count] [lobby]` | Put fake players into the open (or a given scheduled) lobby; default 9, so you plus 9 make ten. |
| `/mock ready [lobby]`, `/mock drop [lobby]` | Make the fake players confirm the ready-check, or leave. |
| `/mock report <winner> [lobby]`, `/mock confirm [lobby]` | A fake player reports a result, or one on the other team confirms it. |
| `/mock stats [match] [validate]` | The fake players of a confirmed game enter random champion and KDA (optionally validated). |
| `/mock list`, `/mock clear` | List the fake players; delete them and the test games they played. |

Mock players have fake Discord IDs, so they cannot be picked as the `user` of `/profile view`,
`/history` or `/stats`; they only appear in lobbies, games and `/mock list`. They are never listed
on `/leaderboard`.

#### Name of the day

| Command | Reply | What it does |
|---|---|---|
| `/goomah` | public | Today's name (Europe/Lisbon date); picked at random on first use of the day. |
| `/goomahdor` | public | Podium of the three most-picked names. |

Both commands also answer to their original names; the answer is identical.

A daily task also makes the pick at 00:00 Europe/Lisbon.
