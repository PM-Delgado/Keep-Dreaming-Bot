# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Report it privately through GitHub:
**Security -> Report a vulnerability** on this repository (private vulnerability reporting). Include what
you found, how to reproduce it and, if you can, what you think the impact is. You will get an answer
as soon as the maintainer can, this being a hobby project.

If you find a leaked Discord token, Riot API key or other credential anywhere in this repository or its
history, report it the same way, and revoke it yourself if it is yours.

## What counts

- A way to run an admin-only command (`ADMIN_USER_IDS`, `/admin`, `/match void|fix|validate`, `/mock`,
  `/sync`) without being allowed to.
- A way to read, change or delete another player's data, or to see raw ratings that are meant to stay
  hidden.
- Leaking `DISCORD_TOKEN` or `RIOT_API_KEY` through logs, error messages or embeds.
- SQL injection or other injection through command options.

Problems in Discord, Riot Games or the libraries the bot depends on should be reported to them.

## Supported versions

Only the latest commit on `main` is supported.

## Running your own copy

Keep `DISCORD_TOKEN` and `RIOT_API_KEY` in environment variables or a git-ignored `.env`, never in the
repository, and leave `ENABLE_MOCK_PLAYERS` off in production. See [docs/self-hosting.md](docs/self-hosting.md).
