# Simulating the matchmaking

You do not need ten friends (or Discord) to see how matchmaking, ratings and ranks behave. Everything
below runs locally, needs no token or network, and is deterministic for a given `--seed`.

```
python -m bot.features.simulation --help
```

| Command | What it does | Speed |
|---|---|---|
| `list` | lists the named scenarios | instant |
| `scenario <name or all>` | one fixed lobby (five junglers, two Masters both Mid, ...), the teams it produces and whether they look as agreed | 1 s each |
| `population` | many random lobbies through the balancer only, summarised | 0.3 s per lobby |
| `league` | random lobbies with the rating maths (OpenSkill, E, ladder) but no database | 0.3 s per game |
| `season` | a whole season **through the real services** (lobby, ready-check, balancer, match, result, rating replay, ladder) in an in-memory database | 8 s for 30 games |
| `tune baseline / sweep / compare / settings / population` | the tuning lab: many seasons of a realistic 20-player group under different settings, in parallel | 20 s for six seasons |

## Scenarios (balancer only)

```
python -m bot.features.simulation scenario all
python -m bot.features.simulation scenario five_junglers --seed 1
```

Each scenario builds ten players with chosen ratings and roles, runs the real balancer, prints the
teams (role, player, rating, how well the role fits) and checks the expectation we agreed
(for example: two Masters end up in the same lane on opposite teams; with five junglers exactly one
plays Jungle per team). Scenarios live in `bot/features/simulation/scenarios.py`; add one there and
the CLI and `tests/test_simulation.py` pick it up.

## Season: the full pipeline

```
python -m bot.features.simulation season --players 14 --games 40 --seed 2
```

Mock players are registered with a Riot-style rank and role comfort, then every game does what the
buttons would: open a lobby, join, ready, balance, confirm the teams, a winner reports and the
other team confirms, ratings are replayed, tiers are derived. Who wins is drawn from **hidden**
truth (true skill, how well the player knows their assigned role, and optional form), never from
the bot's own estimates, so the bot's predictions are graded against a world it cannot read.

The report prints the first games (predicted versus true win chance), a per-player table (true
skill, starting rank, W-L, visible rank or *Colocação n/5*, hidden rating), how well ratings track
true skill, and **invariants** that must always hold:

* stored ratings equal a full replay of the confirmed games;
* every game is confirmed and nobody is left busy;
* games per player add up (ten per match);
* nobody has a tier before their 5th game, everybody has one after.

The exit code is 1 if an invariant breaks, so it works in scripts. `tests/test_season.py` runs the
same checks.

## The tuning lab

```
python -m bot.features.simulation tune settings            # every setting you can change
python -m bot.features.simulation tune population          # the simulated group
python -m bot.features.simulation tune baseline            # the current code defaults
python -m bot.features.simulation tune sweep sigma_rank_seed 2 3 4.5
python -m bot.features.simulation tune compare "tight:d_up=3,d_down=2" "loose:d_up=6"
python -m bot.features.simulation tune compare "oracle:oracle=1"    # ratings equal to the truth
```

Options: `--games 60`, `--seeds 6`, `--workers N` (default: all cores), `--set k=v` (a base
override for every row), `--size 20`, `--deviation-sd 1.2`, `--outlier-share 0.15`, `--form-sd 1.0`,
`--form-persistence 0.5`. Every row uses the same seeds, so a difference comes from the setting.

Any field of `MatchmakingParams`, `EngineParams`, `BalanceParams`, and the ladder's
`placement_games`, `demotion_buffer` and `division_width` can be overridden by name; a typo is an
error. `oracle=1` starts every rating equal to the truth: the best any rating could do.

### The simulated world (assumptions, not measurements)

* About 20 players, Bronze to Master in the proportions 1 : 4 : 5 : 4 : 3 : 2 : 1 (Bronze, Silver,
  Gold, Platinum, Emerald, Diamond, Master). The bot sees only their Riot rank.
* **True skill = the rank estimate R + N(0, 1.2)**, and 15% of players are outliers of about two
  tiers either way (smurfs, inactive old peaks).
* Each game's winner is drawn from the true skill of the assigned roles plus a persistent **form**
  (sd 1.0, carrying over half to the next game) that no rating can see.
* Ten of the twenty play each game, chosen at random; the real balancer builds the teams.

If you believe your group looks different, change `--deviation-sd` and `--outlier-share` and see
whether a conclusion survives.

### Reading the table

| Column | Meaning |
|---|---|
| `lopsided` | share of games where the hidden skills made one side a 65%+ favourite |
| `edge1-20`, `edge-end` | mean distance from 50/50 over the first 20 / last 20 games |
| `p-err` | \|predicted win chance - true win chance\| of the lobbies built |
| `E-rmse: 0 / 10 / 20 / 40 / end` | error of the matchmaking estimate against true skill after that many games. **0 is what the Riot rank alone gives**; above it the in-house ratings are doing worse than trusting the rank |
| `corr` | rank correlation of in-house rating and true skill |
| `tier-err`, `in±1` | distance of the visible tier from the one true skill deserves; share within one step |
| `moves` | visible tier changes per player over the season (flapping) |
| `w/form: lop`, `p-err` | the same two balance numbers counting the day's form |

### Current headline results

See [tuning.md](tuning.md) for the method and every finding. In short, on the default world after
60 games:

| Setting | E-rmse (start -> end) | within one tier | tier changes per player | clearly lopsided games |
|---|---|---|---|---|
| old defaults (`sigma_rank_seed` 6.5, `d_down` 3.0) | 1.82 -> 2.4 | 42% | 13 | 41% |
| **current** (`sigma_rank_seed` 2.0, `d_down` 2.0) | 1.67 -> 1.5 | 67-70% | about 2 | 32-38% |
| perfect ratings (`oracle=1`) | 0 -> 0.14 | 100% | about 0 | 0% |

(Old defaults: 6 seeds. Current and perfect ratings: 10 and 6 seeds. The starting error differs a little
between rows because each uses a different set of simulated groups.)

The perfect-ratings row shows the balancer is not the bottleneck: what is left is *knowing* how
good people are, and balanced games carry little information.

## Adding your own experiment

* A new **setting** is just a field on one of the parameter dataclasses; `tune` finds it by name.
* A new **world assumption** goes in `World` (`simulation/tuning.py`) and `realistic_population`
  (`simulation/population.py`).
* A new **metric** is computed per game in `Season.play_game` (`simulation/season.py`) and reduced
  in `tuning._season`; add the column in `format_summaries`.
* A new **signal** (like the tested last-5-games form) goes in `ratings/matchmaking.py`, is read
  through `RatingService.inspect`, and is switched on with a `MatchmakingParams` field so `tune`
  can sweep it. Keep it off by default until it wins in the lab, and in at least two worlds.

## Tests

`pytest -m "not slow"` skips the long simulations (about 45 s instead of 95 s). The simulation tests
are `tests/test_simulation.py` (scenarios, populations, league), `tests/test_season.py` (the full
pipeline) and `tests/test_tuning.py` (the lab itself).
