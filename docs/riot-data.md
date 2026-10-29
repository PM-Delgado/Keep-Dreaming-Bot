# Riot data: what the bot uses and stores

The Riot integration is optional. Without `RIOT_API_KEY` every `/riot` command answers that the
integration is off, and ranks, role proficiency and ratings keep working manually (`/profile rank`,
`/profile roles`, `/admin rating ...`).

Keep-Dreaming-Bot isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot
Games or anyone officially involved in producing or managing Riot Games properties. Riot Games and
all associated properties are trademarks or registered trademarks of Riot Games, Inc. The in-house
ranks the bot shows are internal to the group and are not League of Legends ranks.

If you run your own copy you are responsible for registering with the
[Riot Developer Portal](https://developer.riotgames.com) and following Riot's current
[policies](https://developer.riotgames.com/policies/general). A development key expires after 24
hours; use a registered personal key for ongoing use.

## What is requested

Per linked account (`/riot link Name#TAG`):

| Call | Used for |
|---|---|
| account-v1 by-riot-id / by-puuid | resolve a Riot ID to a PUUID and keep the displayed Riot ID current |
| summoner-v4 by-puuid | check the account has a League profile on the chosen platform |
| league-v4 entries by-puuid | the main account's Solo/Duo rank: the starting rating and the rank-based estimate used by matchmaking |
| match-v5 ids + match (queue 420, ranked Solo/Duo only) | role experience: which roles the player has recently played |

Custom games are never requested from Riot. In-house results and KDA are entered manually.

Account linking is an honour system: the bot checks that the Riot ID resolves, not that the person
owns it. Linked account names are visible to everyone in the server.

## What is stored

| Table | Content | Removed on `/riot unlink` |
|---|---|---|
| `riot_accounts` | PUUID, current Riot ID, platform, main flag, who linked it and when | yes |
| `riot_role_games` | per ranked Solo/Duo game: id, time, position, win, K/D/A, duration, champion, CS and a few totals; shown by `/riot history` | yes |
| `rank_signals` (source `riot`) | the account's Solo/Duo tier and division over time (feeds the in-house rating) | yes |
| `riot_accounts.rank_*` | the account's last looked-up Solo/Duo tier, division and LP (leaderboard); empty tier = unranked | yes (the row) |
| `riot_rank_snapshots` | one row per main account per day (tier, division, LP), written when the daily leaderboard is posted, for the LP trend | yes (cascade) |
| `rating_events` | numeric rating seeds; never contains a Riot ID | no |

After an unlink nothing in the database references the PUUID, and ratings are unaffected. Only the
current Riot ID is stored; old ones are not kept.

## When Riot is asked

Nobody has to run `/riot refresh`: the same calls are made automatically, each limited by how old the
saved data is.

| Trigger | What it asks | Only if the last check is older than |
|---|---|---|
| `/profile view` | each account's Riot ID, the main account's rank | 30 minutes |
| `/riot history` | new ranked games of the accounts shown | 5 minutes (per account) |
| A ready-check starts | the main account's rank of each of the ten players | 60 minutes |
| A timer, every 6 hours | Riot IDs and rank of every linked account | every account, in the background |
| The daily leaderboard (10:00 Lisbon, `RIOT_FEED_CHANNEL_ID` set) and `/riot leaderboard` | the Solo/Duo tier, division and LP of every **main** account (one league-v4 request each, background) | daily: always; `/riot leaderboard`: a main missing or older than 1 hour (waits up to 10 seconds) |

A failed lookup is only logged and the saved data is shown. If Riot rejects the key, the bot logs a
warning and messages the admins listed in `ADMIN_USER_IDS` at most once a day.

## Rate limits and the key

`RIOT_API_KEY` is read once in `bot/config.py`, sent only in the `X-Riot-Token` header, excluded from
`Settings.__repr__` and never written to logs or error messages (a test checks this). The client stays
under the personal-key limits (18 requests per second, 90 per 2 minutes), honours `Retry-After` on
HTTP 429, and reserves part of each window for interactive commands so background syncs never starve
them.

## Champion, role and rank icons

Champion icons come from Riot's Data Dragon (`ddragon.leagueoflegends.com`); the five role icons in
`bot/features/icons/roles/` were rendered from the game client's position SVGs as published by
[Community Dragon](https://www.communitydragon.org). Each is uploaded once as a Discord application
emoji. The ten rank icons in `bot/features/icons/tiers/` are the ranked emblems published by Community Dragon, cropped and resized to 128 px, uploaded the same way (`tier_emojis`) and shown in the Solo/Duo leaderboard; without an uploaded icon a colour dot is shown. Set `CHAMPION_ICONS=false` to stop uploading and showing all of them; names are used instead.
