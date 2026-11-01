"""Plain-text report of a backtest. Pure."""

from collections.abc import Sequence

from bot.features.backtest.model import Dataset, Variant, VariantResult
from bot.features.backtest.runner import brier, check_against_stored, compare, log_loss

COIN_LOG_LOSS = 0.6931
COIN_BRIER = 0.25
# A gain smaller than this many standard errors is not evidence of anything.
SIGNIFICANT = 2.0


def describe(variant: Variant) -> str:
    parts = []
    if variant.loss_scale != 1.0:
        parts.append(f"loss x{variant.loss_scale:g}")
    if variant.role_aware:
        parts.append("role-aware")
    if variant.sigma_floor:
        parts.append(f"sigma floor {variant.sigma_floor:g}")
    if variant.d_up is not None:
        parts.append(f"d_up {variant.d_up:g}")
    if variant.d_down is not None:
        parts.append(f"d_down {variant.d_down:g}")
    return ", ".join(parts) or "as the bot works now"


def summary(data: Dataset, results: Sequence[VariantResult]) -> str:
    baseline = results[0]
    games = len(data.games)
    lines = [
        f"{games} confirmed games, {len(baseline.final)} players with a rating.",
    ]
    diff = check_against_stored(data, baseline)
    if diff is None:
        lines.append("Check: no stored ratings to compare the baseline replay with.")
    elif diff < 1e-6:
        lines.append("Check: the baseline replay reproduces the bot's stored ratings exactly.")
    else:
        lines.append(
            f"WARNING: the baseline replay differs from the stored ratings by up to {diff:.4f}."
            " Either the stored caches are stale or the bot's algorithm is not the one this"
            " tool replays; do not trust the comparison until this is explained."
        )
    if baseline.unrecorded:
        lines.append(
            f"Note: {baseline.unrecorded} player-games have no recorded role profile and are"
            " treated as unknown."
        )
    if games < 20:
        lines.append(
            f"With only {games} games, treat every difference below as a hint, not a finding."
        )
    lines += [
        "",
        f"{'variant':44s} {'log-loss':>8s} {'brier':>7s} {'gain/game':>10s}"
        f" {'+-se':>6s} {'better':>7s}",
    ]
    for r in results:
        what = describe(r.variant)
        name = r.variant.name if r.variant.name == what else f"{r.variant.name} ({what})"
        row = f"{name[:44]:44s} {log_loss(r):8.4f} {brier(r):7.4f}"
        if r is baseline:
            row += f" {'-':>10s} {'-':>6s} {'-':>7s}"
        else:
            p = compare(baseline, r)
            se = "n/a" if p.stderr is None else f"{p.stderr:.3f}"
            mark = ""
            if p.stderr and abs(p.mean_gain) >= SIGNIFICANT * p.stderr:
                mark = " *"
            row += f" {p.mean_gain:+10.4f} {se:>6s} {p.better:>3d}/{p.games:<3d}{mark}"
        lines.append(row)
    lines += [
        "",
        f"A coin flip scores log-loss {COIN_LOG_LOSS} and brier {COIN_BRIER}; lower is better.",
        "gain/game: average improvement in log-likelihood over the first row (positive = the",
        f"variant would have predicted the real results better). '*' = at least {SIGNIFICANT:g}"
        " standard errors,",
        "which assumes the games are independent of each other: the same few players keep",
        "playing, so even a '*' is weaker evidence than it looks.",
    ]
    return "\n".join(lines)


def detail(data: Dataset, baseline: VariantResult, other: VariantResult) -> str:
    """Per player: H and visible tier under the baseline and under `other`."""
    lines = [
        f"Players under '{other.variant.name}' compared with the baseline (H = in-house rating):",
        f"{'player':16s} {'games':>5s} {'H now':>7s} {'H variant':>9s} {'diff':>6s}"
        "  tier now -> tier variant",
    ]
    rows = []
    for pid, base in baseline.final.items():
        if base.games == 0:
            continue
        var = other.final[pid]
        rows.append((var.mu - base.mu, data.names.get(pid, str(pid)), base, var))
    for delta, name, base, var in sorted(rows, key=lambda r: (r[0], r[1])):
        flag = "   <- tier changes" if base.label != var.label else ""
        lines.append(
            f"{name[:16]:16s} {base.games:5d} {base.mu:7.2f} {var.mu:9.2f} {delta:+6.2f}"
            f"  {base.label} -> {var.label}{flag}"
        )
    return "\n".join(lines)
