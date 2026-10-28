# How ratings, ranks and matchmaking work

This is the reference for the in-house system: where a player's strength comes from, how a
game changes it, what the visible rank means, and how teams are built. Numbers are the code
defaults at the time of writing; the file that owns each one is named so you can check it.

Contents: [the three numbers](#the-three-numbers-r-h-e) · [where R comes from](#where-r-comes-from) ·
[how a game changes H](#how-a-game-changes-h) · [the visible rank](#the-visible-rank) ·
[roles](#roles-and-proficiency) · [building teams](#building-teams) ·
[the life of a game](#the-life-of-a-game) · [what is stored](#what-is-stored-and-what-is-derived) ·
[parameter reference](#parameter-reference) · [why these defaults](#why-these-defaults)

## The three numbers: R, H, E

Every registered player has three strength numbers on one scale (roughly 15 to 35; a default
newcomer is 25). Only admins ever see them (`/admin rating inspect`).

| | Name | What it is | Changes when |
|---|---|---|---|
| **R** | rank estimate | what the player's League rank says, on our scale | their Riot rank (or an admin/self-reported rank) changes |
| **H** | in-house rating | OpenSkill `(mu, sigma)`: how good they are *in our games* | a confirmed in-house game is played (or an admin adjusts it) |
| **E** | matchmaking estimate | what the balancer actually uses | whenever R or H change (it is computed, never stored) |

```
E = R + D * tanh((H - R) / D)        D = d_up (4.5) above R, d_down (2.0) below R
```

* Small differences pass straight through (`E` is about `H`).
* However long a streak lasts, `E` stays between `R - 2.0` and `R + 4.5`. A Master on a ten-game
  losing streak is still treated as a strong player; a Silver on a winning streak is not treated as
  a Master. The bounds are asymmetric because friends' Riot ranks are more often *below* their real
  level than above it.
* With no rank, `E = H`. Before any in-house game, `H = R`, so `E = R`.
* The uncertainty is shared: `sigma_E = sigma_H`.

Code: `bot/features/ratings/matchmaking.py` (E), `params.py` (all defaults), `engine.py` (the only
file that imports OpenSkill).

## Where R comes from

R is derived from the Riot **Solo/Duo** rank of the player's *main* linked account:

```
R = 20.0 + 0.4 * points        points = tier_index * 4 + division_index
```

| Rank | Points | R | | Rank | Points | R |
|---|---|---|---|---|---|---|
| Iron IV | 0 | 20.0 | | Platinum IV | 16 | 26.4 |
| Bronze IV | 4 | 21.6 | | Emerald IV | 20 | 28.0 |
| Silver IV | 8 | 23.2 | | Diamond IV | 24 | 29.6 |
| Gold IV | 12 | 24.8 | | Diamond I | 27 | 30.8 |
| Gold I | 15 | 26.0 | | Master | 28 | 31.2 |

Division order inside a tier is IV, III, II, I (lowest to highest). Master, Grandmaster and
Challenger ignore division and LP (31.2, 32.8, 34.4). A tier is deliberately compressed to 1.6
points so that in-house results can still separate players of the same rank.

Which rank counts is decided in this order (`ratings/rank.py`, `resolve_rank`):

1. **Admin override** (`/admin rating rank`), until an admin clears it.
2. **Riot, main account only.** A successful refresh that finds a rank uses it; one that finds the
   player *unranked* replaces any older ranked reading. Failed requests write nothing, so an outage
   can never erase a good rank. Other linked accounts never add rank evidence.
3. **Self-reported** (`/profile rank`).
4. **None**: E = H.

A Riot rank older than 30 days is flagged "desatualizado" in the profile and in admin diagnostics.
Staleness does **not** change E.

## How a game changes H

* **Seed.** A new player starts with `H = R` and `sigma = 2.0` (a rank is real evidence). Without a
  rank they start at `mu 25, sigma 8.33`. A seed is stored as a number, so changing the rank scale
  later never rewrites an existing seed.
* **Re-seed.** If the player's resolved rank changes while they have fewer than 5 confirmed games,
  a new seed is written (there is no in-house evidence to protect yet). From 5 games on, only E
  moves with the new rank; H is left alone.
* **Update.** Only *confirmed* results change H, using OpenSkill Plackett-Luce with `beta = 4.17`
  and `tau = 0.083`: winners go up, losers down, bigger moves for upsets and for uncertain
  players. KDA, champion and role never enter the formula.
* **Replay.** H is never edited in place. It is recomputed by replaying every seed, admin
  adjustment and confirmed game in order (`ratings/replay.py`). Confirming, correcting or voiding a
  game replays everything inside the same database transaction; 500 games take about 80 ms.
  `/admin rating recompute` does the same on demand.

## The visible rank

The visible rank is derived from **H** (not E, not R), so it reflects performance in our games.

| Tier | Floor of its lowest division | Divisions |
|---|---|---|
| Bot Máximo | 18 | III, II, I |
| Rush Caixa | 21 | III, II, I |
| Duro que nem um rijo | 24 | III, II, I |
| No Box | 27 | III, II, I |
| KD esports | 30 | none |

* Divisions are one point of H apart (Bot Máximo III = 18, II = 19, I = 20, then Rush Caixa III = 21).
  Below 18 clamps to Bot Máximo III; 30 and above is KD esports. A newcomer at 25 is Duro que nem um
  rijo II; a Gold IV seed (24.8) starts at Duro III and a Diamond I seed (30.8) at KD esports.
* **Placement:** nobody has a tier until their **5th confirmed game**. Profiles show
  *Colocação n/5* instead. Before that the player still has a full rating and is matched by it.
* **Promotion** happens at the division floor. **Demotion** only after H falls `0.33` below the
  current division's floor, so a player on the boundary does not flicker.
* The profile and the group leaderboard show a progress bar inside the division (full on the top
  step), never a number; the leaderboard orders players inside a division by it.
* After each confirmed game the channel gets a summary listing promotions, demotions, finished
  placements and placement progress (no ratings).

Thresholds are provisional guesses: `ratings/ladder.py` (`DEFAULT_LADDER`), tunable with
`python tools/ladder_sim.py` and the simulations (see [simulations.md](simulations.md)). Stored
data uses stable tier keys (`bot_maximo`, ...), never display names, so renaming a tier touches no
history.

## Roles and proficiency

Players **never choose a role per game**. Roles come from a persistent proficiency per role
(1 to 5, or unknown), with this precedence: admin override, then the player's own declaration
(`/profile roles`), then an estimate from Riot ranked history (capped at 4), then unknown.

The estimate counts the player's recent ranked Solo/Duo games per role from all linked accounts
(half-life 90 days, last 100 games per account within 12 months, remakes dropped). With enough
confidence it maps weighted games on a role to a level: 20 or more is 4, 8 or more is 3, 3 or more
is 2, anything else is 1. With too little data every role is *unknown* rather than low.

## Building teams

When ten players are ready, the balancer (`bot/features/balance/`, pure and Discord-free)
searches all 126 ways to split them into two teams and, for each team, the best assignment of
Top/Jungle/Mid/ADC/Support. It minimises one score:

```
score = 1.0   * |P(blue wins) - 0.5|
      + 0.015 * sum of role costs
      + 0.004 * sum of lane gaps
      + 0.005 * gap between the teams' top-2 sums
      + 0.01  * gap between the teams' spread (stdev of E)
      + 0.10  if the three strongest players are all on one team
```

* **P(blue wins)** comes from `E` (and sigma) of the ten players with each player's strength
  *adjusted for the role they would play*: a player loses 0.3 / 0.8 / 1.8 / 3.0 points at
  proficiency 4 / 3 / 2 / 1 (1.0 if unknown).
* **Role cost** is what playing a role costs the player: 0 / 0.5 / 1.0 / 2.5 / 4.0 at proficiency
  5 / 4 / 3 / 2 / 1, and 1.5 if unknown. With five junglers, one plays Jungle per team and the
  others go to their next-best role (the proposal marks them `4/5`).
* **Lane gap**: equal-strength players are placed against each other in the same lane, so two
  Masters both meet in Mid (or both go off-lane) instead of one crushing a weak lane.
* **Top-2 / spread / elite** terms keep the strongest players spread across both teams.
* Among proposals within 0.002 of the best, one is picked at random for variety. There is **no
  reroll**: cancel and reopen the lobby to get a different split.
* The search is exact for the winning split and takes about 0.2 s, run in a worker thread.

Players only see a coarse label, from the win-probability gap: *Equilibrado* (under 3 points off
50%), *ligeira vantagem* (3-7), *vantagem clara* (7-15), *muito desequilibrado* (15 or more).

Code: `balance/engine.py` (search), `balance/model.py` (`BalanceParams`, the numbers above).

## The life of a game

```
OPEN -> READY_CHECK -> TEAMS_PROPOSED -> IN_PROGRESS -> COMPLETED
  |          |               |               |
  +----------+---------------+---------------+--> CANCELLED
```

1. **Open / schedule.** `/lobby open` or `/lobby schedule`; people join with buttons. A scheduled
   lobby reminds its sign-ups 30 minutes before and starts its ready-check at the chosen time.
2. **Ready-check** (default 2 minutes) at ten players. Silent players are dropped and replaced
   from the reserves, otherwise the lobby returns to OPEN.
3. **Teams proposed** by the balancer; the creator or an admin confirms. That creates the *match*
   and freezes what the balancer saw (H, E, R per player).
4. **Result.** A participant reports the winner; someone on the other team confirms or contests.
   No objection for 30 minutes confirms it automatically; a contested result waits for an admin
   (`/match fix` or the winner buttons). An admin's report is final at once. After 90 minutes
   without a report, players are reminded; after 12 hours the game is voided.
5. **Confirmed**: all ratings are recomputed in one transaction, the lobby completes, and the
   channel gets the promotion/placement summary.
6. **Corrections**: `/match void` (undoes the effect if it had been confirmed) and `/match fix`
   (flips a confirmed result) replay everything again. Every step is kept in an audit trail
   (the `match_audit` table; `/match show` does not display it, only who confirmed and validated).
7. **Champion and KDA** (after step 5 only). Each player records their champion and
   kills/deaths/assists with `/match stats`; that entry is *pending* until an admin validates it
   (`/match validate`, `/match pending` lists them). Admin entries (`/match set`) are validated at
   once and can also record the role a player actually played. Editing validated stats sends them
   back to pending. Stats are audited like results, never change a result or a rating, and only
   only validated stats feed the public numbers (`/history`, `/stats`, `/leaderboard`). The champion list comes from Data Dragon,
   cached in the database (with a bundled list for the first start).

Players in a started game cannot join another lobby until its result is in. Signing up to a
*future* scheduled lobby is always allowed.

## What is stored and what is derived

| Stored (authoritative) | Derived (rebuildable) |
|---|---|
| `rating_events` (seeds, admin adjustments), `rank_signals`, `matches` + `match_participants` + `match_audit`, `player_roles`, Riot accounts and role games | `ratings`, `rating_history` (replay of the left column), E (computed on demand), the visible rank |

Changing a tunable (`d_up`, the ladder, ...) never rewrites history: E and the visible rank are
recomputed from the stored inputs. Changing how H evolves (`beta`, `tau`) bumps the algorithm
version, and the caches are replayed at startup.

## Parameter reference

| Setting | Default | Where | Meaning |
|---|---|---|---|
| `rank_mu_base`, `rank_mu_step` | 20.0, 0.40 | `MatchmakingParams` | rank to R scale |
| `default_mu`, `default_sigma` | 25, 8.33 | `MatchmakingParams` | newcomer with no rank |
| `sigma_rank_seed` | 2.0 | `MatchmakingParams` | uncertainty of a rank-seeded H |
| `d_up`, `d_down` | 4.5, 2.0 | `MatchmakingParams` | how far E may sit above/below R |
| `reseed_below_games` | 5 | `MatchmakingParams` | rank change re-seeds H below this many games |
| `rank_stale_after_days` | 30 | `MatchmakingParams` | flag only |
| `form_weight` (+ `form_games`, `form_cap`) | 0 (off), 5, 2.0 | `MatchmakingParams` | last-5-games form signal; tested, no gain |
| `beta`, `tau` | 4.17, 0.083 | `EngineParams` | OpenSkill |
| `placement_games` | 5 | `LadderConfig` | games before a tier is shown |
| `demotion_buffer`, `division_width` | 0.33, 1.0 | `LadderConfig` | hysteresis, division size |
| `w_role`, `w_lane`, `w_topk`, `w_spread` | 0.015, 0.004, 0.005, 0.01 | `BalanceParams` | balancer score terms |
| `elite_count`, `elite_penalty` | 3, 0.10 | `BalanceParams` | star-spreading rule |
| `ready_check_seconds`, `idle_hours` | 120, 3 | `LobbyParams` | ready-check length; an idle unfinished lobby is cancelled |
| `reminder_minutes` (scheduled lobby) | 30 | `LobbyParams` | reminder before a scheduled start |
| `auto_confirm_minutes`, `reminder_minutes`, `expire_hours` | 30, 90, 12 | `MatchParams` | quiet result is confirmed; unreported game is nudged, then voided |

## Why these defaults

The defaults come from the tuning lab (see [simulations.md](simulations.md#the-tuning-lab)): many
full seasons of a made-up group of about twenty players, played through the real lobby, match and
rating code, with settings compared on the same seeds. The simulated world is an assumption, not a
measurement. Unless noted, the numbers below are 60 games and 6 seeds.

### Findings

1. **Perfect ratings make the balancer nearly perfect.** With `oracle=1`, 0% of games are lopsided
   and the mean edge is 0.012. Everything left is a question of *knowing* how good people are.
2. **The first defaults made ratings worse than the rank alone.** Estimate error went from 1.82
   (rank only) to 2.3 after 10-60 games, only 42% of players ended within one tier of the right one,
   and a player changed visible tier 13 times in 60 games. Cause: a rank-seeded rating started with
   `sigma_rank_seed = 6.5`, so each of the first games moved ratings by several points, and a
   balanced game carries very little information (it is close to a coin flip).
3. **`sigma_rank_seed` is the big lever.** Trusting the rank more (2-3 instead of 6.5) turns the
   curve around: error falls from 1.82 to about 1.5 after 40-60 games, 65-72% of players end within
   one tier, and tier changes drop from 13 to 1-3 per player. `beta` (8-12) and `d_down` (2) help a
   little and mostly reduce flapping; `d_up` matters little.
4. **A joint search (73 settings x 2 worlds) finds a flat top.** The best dozen settings are within
   noise of each other, all with `sigma_rank_seed` 2-3, so the simplest was picked.
5. **In a harsher world** (rank deviation sd 2.0, 30% outliers) no setting beats the first defaults
   on estimate error by much (2.3 vs 2.3-2.5 after 60 games), but the new ones still end with 1-3
   tier changes per player instead of 13. If real ranks say little about real skill, a larger
   `sigma_rank_seed` (about 3) is the safer middle. Revisit after about 100 real games.
6. **The remaining error is mostly a data limit.** Even tuned, 30-38% of games are clearly lopsided
   on the hidden skills after 60 games. A balanced game is nearly a coin flip, so it teaches the
   system little; the first ~20 games (about 7 per player) change almost nothing. More *signals* are
   the way forward, not more parameter tweaking.

### Settings applied

| Setting | Was | Now | Why |
|---|---|---|---|
| `sigma_rank_seed` | 6.5 | 2.0 | trust the Riot rank until results say otherwise |
| `d_down` | 3.0 | 2.0 | a slump should not drag the matchmaking estimate far below the rank |
| `beta`, `d_up`, `tau`, ladder | unchanged | unchanged | no clear gain in the search |

Changing these does not rewrite history: `sigma_rank_seed` only affects *new* seeds and `d_down` is
applied when the estimate is computed. Players already seeded keep their stored sigma.

### Experiment: form from the last five games (no gain, left off)

The signal: the average of (won - the win chance the bot predicted) over a player's last `form_games`
confirmed games, times `form_weight` rating points (capped at `form_cap`), added to the matchmaking
estimate E only, never to H or the visible tier. The simulated world has hidden form that the bot
cannot see, so the signal has something real to find. Each cell is the share of clearly lopsided
games counting the day's form / the prediction error (10 seeds, 60 games, current defaults):

| Hidden form | Weight 0 (off) | Weight 2 | Weight 4 | Weight 8 |
|---|---|---|---|---|
| sd 1, fades fast (a normal day-to-day swing) | 40% / .140 | 43% / .143 | 45% / .149 | 47% / .158 |
| sd 3, fades fast | 64% / .222 | 66% / .232 | 64% / .221 | 67% / .235 |
| sd 2.5, streaky (carries 80%) | 57% / .193 | | 61% / .202 | 60% / .199 |
| sd 3, very streaky (carries 95%) | 64% / .216 | | 63% / .214 | 65% / .220 |

A window of 3 or 10 games gave the same picture. The signal is never clearly better than nothing and
is usually a little worse: one balanced game is nearly a coin flip, so five of them say very little
about someone's form, and the adjustment mostly adds noise. It stays at `form_weight = 0`. The code
is small and tested, so a better signal can reuse it.

### Ideas to test next

* A stronger per-game performance signal than win/loss, now that kills/deaths/assists are recorded
  and validated per game: a game's KDA says more about a player's day than a coin-flip result.
* Riot ranked history (recent win rate, games played, LP trend) as an extra prior; stale or inactive
  ranks widen the uncertainty.
* Adaptive uncertainty: raise a player's sigma when they keep beating or losing to the prediction.
* Visible-tier thresholds and hysteresis (`demotion_buffer`, tier floors) once ratings are stable.
* Balancer weights (`w_role`, `w_lane`, ...) in the harsher world, where fit matters more.
