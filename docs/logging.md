# Logging

The bot writes the same lines to the console and to `logs/kdbot.log` (git-ignored; yesterday's
file is `kdbot.log.<date>`). Each line is `date time LEVEL logger: message`.

* **Startup:** the switches in effect (never a token or key), the database file, its schema
  version and how many names, phrases, players, lobbies, games and champions it holds, each
  migration, the seeds loaded into an empty `/goomah`, each feature loaded, the command sync, and
  `Ready` with the server name. Shutdown is logged too.
* **Every command, button and form:**
  `/lobby open by ana (123) in #scrims (456) [lobby_id=3] -> ok 84ms`. The outcome is `ok`,
  `refused (reason)` when a rule said no, `denied` for a missing permission, or `error` with the
  traceback. Buttons log when pressed, e.g. `button lobby:join ... on message 789`.
* **What changed:** lobby events (`Lobby 5 (ready_check): ready_check_started, roster_changed`),
  registrations, rank signals, the daily name pick, the lobby sweeper, Riot and champion refreshes.

To follow it live: `Get-Content logs\kdbot.log -Wait -Tail 50` (PowerShell) or
`tail -f logs/kdbot.log`. Warnings and errors: `Select-String -Path logs\kdbot.log -Pattern "WARNING|ERROR"`.
