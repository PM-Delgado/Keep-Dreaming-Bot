## What and why

<!-- What does this change, and why? Link the issue if there is one. -->

## Checklist

- [ ] `ruff check . && ruff format --check .`, `pyright` and `pytest` pass
- [ ] Tests added or updated for changed behaviour
- [ ] Docs updated if a command, option or output changed (`docs/commands.md`, README)
- [ ] No tokens, `.env` files, databases or real Discord IDs
- [ ] A schema change is a new migration in `bot/db.py` (no applied migration was edited)
