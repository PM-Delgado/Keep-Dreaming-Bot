# Tuning the matchmaking and ratings

The tuning lab plays many full seasons of a made-up group through the real lobby, match and rating
code (in memory, no Discord) and compares settings. Everything below is reproducible:

```
python -m bot.features.simulation tune settings          # the settings you can change
python -m bot.features.simulation tune population         # the group that is simulated
python -m bot.features.simulation tune baseline           # the current code defaults
python -m bot.features.simulation tune sweep sigma_rank_seed 2 3 4.5
python -m bot.features.simulation tune compare "a:sigma_rank_seed=2,d_down=2" "b:beta=8"
python -m bot.features.simulation tune compare "oracle:oracle=1"   # ratings equal to the truth
```

Useful options: `--games 120`, `--seeds 10`, `--deviation-sd 2.0 --outlier-share 0.3` (a world
where Riot ranks say less about real skill), `--form-sd 0` (no hidden form), `--set k=v` (a base
override for every row). Every row uses the same seeds, so differences come from the setting.

## How the simulated world works (assumptions, not measurements)

* About twenty players, Bronze to Master in the proportions 1:4:5:4:3:2:1 (Bronze, Silver, Gold,
  Platinum, Emerald, Diamond, Master). The bot sees only their Riot rank.
* **True skill = the rank-based estimate + N(0, 1.2)**, and 15% of players are outliers of about
  two tiers either way (smurfs, inactive old peaks). Nobody tells the bot who they are.
* Each game's winner is drawn from the *hidden* truth: true skill, how well the player knows the
  role they were given, and a persistent **form** (sd 1.0, carries over 50% to the next game) that
  no rating can see. Predictions are never graded against the bot's own estimates.
* Ten of the twenty play each game, chosen at random; teams are built by the real balancer.

## What the columns mean

* `lopsided`: share of games where the hidden skills made one side a clear favourite (65%+).
* `edge`: how far from 50/50 the games really were (first 20 games / last 20).
* `p-err`: |predicted win chance - true win chance| of the lobbies the bot built.
* `E-rmse`: error of the matchmaking estimate against true skill (rating points) at 0, 10, 20, 40
  games and at the end. **0 is what the Riot rank alone gives.** Below it, results help; above it,
  the in-house ratings are making things worse than just trusting the rank.
* `corr`: rank correlation of in-house rating and true skill.
* `tier-err`, `in±1`: distance of the visible tier from the one the true skill deserves; share of
  placed players within one step.
* `moves`: visible tier changes per player over the season (flapping).

## Findings (60 games, 6 seeds unless noted; items 2-3 compare the old defaults with the new)

1. **Perfect ratings make the balancer nearly perfect**: with `oracle=1`, 0% of games are lopsided
   and the mean edge is 0.012. Everything left is a question of *knowing* how good people are.
2. **The old defaults made ratings worse than the rank alone.** Estimate error went from 1.82 (rank
   only) to 2.3 after 10-60 games, only 42% of players ended within one tier of the right one, and
   a player changed visible tier 13 times in 60 games. Cause: a rank-seeded rating started with
   `sigma_rank_seed = 6.5`, so each of the first games moved ratings by several points, and a
   balanced game carries very little information (a coin flip).
3. **`sigma_rank_seed` is the big lever.** Trusting the rank more (2-3 instead of 6.5) turns the
   curve around: error falls from 1.82 to about 1.5 after 40-60 games, 65-72% of players end
   within one tier, and tier changes drop from 13 to 1-3 per player. `beta` (8-12) and `d_down`
   (2) help a little and mostly reduce flapping; `d_up` matters little.
4. **A joint search (73 settings x 2 worlds, 6 seeds) finds a flat top**: the best dozen settings
   are within noise of each other, all with `sigma_rank_seed` 2-3. Pick the simplest.
5. **In a harsher world** (rank deviation sd 2.0, 30% outliers) no setting beats the old defaults
   on estimate error by much (2.3 vs 2.3-2.5 after 60 games), but the new ones still end with
   1-3 tier changes per player instead of 13. If real ranks turn out to say little about real
   skill, a larger `sigma_rank_seed` (about 3) is the safer middle. Revisit after ~100 real games.
6. **The remaining error is mostly a data limit.** Even tuned, 30-38% of games are clearly lopsided
   on the hidden skills after 60 games. A balanced game is nearly a coin flip, so it teaches the
   system little; the first ~20 games (about 7 per player) change almost nothing. More *signals*
   are the way forward, not more parameter tweaking (see below).

## Settings applied

| setting | was | now | why |
|---|---|---|---|
| `sigma_rank_seed` | 6.5 | 2.0 | trust the Riot rank until results say otherwise |
| `d_down` | 3.0 | 2.0 | a slump should not drag the matchmaking estimate far below the rank |
| `beta`, `d_up`, `tau`, ladder | unchanged | unchanged | no clear gain in the search |

Changing these does not rewrite history: `sigma_rank_seed` only affects *new* seeds and `d_down` is
applied when the estimate is computed. Players already seeded keep their stored sigma.

## Experiment: form from the last five scrims (result: no gain, left off)

The signal: the average of (won - the win chance the bot predicted) over a player's last
`form_games` confirmed games, times `form_weight` rating points (capped at `form_cap`), added to
the matchmaking estimate E only. Never H, never the visible tier. The world has hidden form that the
bot cannot see, so the signal has something real to find. Games decided with the day's form
(`w/form: lop` = share clearly lopsided counting form; `p-err` = prediction error against the odds
that decided the game; 10 seeds, 60 games, new defaults):

| hidden form | weight 0 (off) | weight 2 | weight 4 | weight 8 |
|---|---|---|---|---|
| sd 1, fades fast (like a normal day-to-day swing) | 40% / .140 | 43% / .143 | 45% / .149 | 47% / .158 |
| sd 3, fades fast | 64% / .222 | 66% / .232 | 64% / .221 | 67% / .235 |
| sd 2.5, streaky (carries 80%) | 57% / .193 | | 61% / .202 | 60% / .199 |
| sd 3, very streaky (carries 95%) | 64% / .216 | | 63% / .214 | 65% / .220 |

A window of 3 or 10 games gave the same picture. The signal is never clearly better than nothing and
is usually a little worse: one balanced game is nearly a coin flip, so five of them say very little
about someone's form, and the adjustment mostly adds noise. It stays at `form_weight = 0`. The code
is small and tested, so a better signal can reuse it.

## Ideas to test next

* **A stronger per-game performance signal** than win/loss, once kills/deaths/assists are recorded
  (M3b, now recorded and validated per game): a game's KDA says more about a player's day than a
  coin-flip result does.
* Riot ranked history (recent win rate, games played, LP trend) as an extra prior; stale or
  inactive ranks widen the uncertainty.
* Adaptive uncertainty: raise a player's sigma when they keep beating or losing to the prediction.
* Visible-tier thresholds and hysteresis (`demotion_buffer`, tier floors) once ratings are stable.
* Balancer weights (`w_role`, `w_lane`, ...) in the harsher world, where fit matters more.
