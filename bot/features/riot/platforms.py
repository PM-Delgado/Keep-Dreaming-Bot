"""Riot platform IDs and the regional routing host each one belongs to."""

# platform -> regional routing value for match-v5 (and, via account_region, account-v1).
PLATFORM_REGION = {
    "br1": "americas",
    "la1": "americas",
    "la2": "americas",
    "na1": "americas",
    "jp1": "asia",
    "kr": "asia",
    "eun1": "europe",
    "euw1": "europe",
    "ru": "europe",
    "tr1": "europe",
    "oc1": "sea",
    "ph2": "sea",
    "sg2": "sea",
    "th2": "sea",
    "tw2": "sea",
    "vn2": "sea",
}
PLATFORMS = tuple(PLATFORM_REGION)


def normalise_platform(value: str) -> str:
    platform = value.strip().lower()
    if platform not in PLATFORM_REGION:
        raise ValueError(f"Unknown platform: {value!r}")
    return platform


def match_region(platform: str) -> str:
    """Regional host for match-v5."""
    return PLATFORM_REGION[normalise_platform(platform)]


def account_region(platform: str) -> str:
    """Regional host for account-v1, which is served from americas/asia/europe only."""
    region = match_region(platform)
    return "asia" if region == "sea" else region
