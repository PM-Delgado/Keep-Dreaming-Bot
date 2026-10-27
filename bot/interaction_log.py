"""One log line per thing a person does in Discord: a slash command, a button, a form.

Who, where, what (with the options) and how it ended (ok / refused / denied / error) and how long
it took. It only reads from the interaction; nothing here changes how a command behaves.
"""

import functools
import logging
import time
from collections.abc import Callable
from typing import Any, ParamSpec

import discord
from discord import app_commands

log = logging.getLogger("bot.interactions")

MAX_VALUE = 80
_STARTED = "kd_started"
_REFUSED = "kd_refused"


P = ParamSpec("P")


def _never_raises(func: Callable[P, None]) -> Callable[P, None]:
    """Logging must never break a command: whatever goes wrong here is dropped."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            func(*args, **kwargs)
        except Exception:
            log.debug("Could not log an interaction", exc_info=True)

    return wrapper


@_never_raises
def mark_received(interaction: discord.Interaction) -> None:
    interaction.extras.setdefault(_STARTED, time.monotonic())


def who(interaction: discord.Interaction) -> str:
    return f"{interaction.user.name} ({interaction.user.id})"


def where(interaction: discord.Interaction) -> str:
    channel = interaction.channel
    name = getattr(channel, "name", None)
    return f"#{name} ({interaction.channel_id})" if name else str(interaction.channel_id)


def _value(value: Any) -> str:
    if isinstance(value, discord.Attachment):
        return f"file:{value.filename}"
    ident = getattr(value, "id", None)
    name = getattr(value, "name", None)
    if ident is not None and name is not None:  # a user, role or channel option
        return f"{name} ({ident})"
    text = str(value)
    if len(text) > MAX_VALUE:
        text = text[: MAX_VALUE - 1] + "…"
    return repr(text) if isinstance(value, str) else text


def options_of(interaction: discord.Interaction) -> str:
    try:
        pairs = [(name, value) for name, value in interaction.namespace]
    except Exception:  # never let logging break a command
        return ""
    return " ".join(f"{name}={_value(value)}" for name, value in pairs)


def describe(interaction: discord.Interaction) -> str:
    """`/lobby open`, `button lobby:join`, `form match:stats`..."""
    if interaction.type is discord.InteractionType.application_command:
        command = interaction.command
        return f"/{command.qualified_name}" if command else "/?"
    data = interaction.data or {}
    custom_id = data.get("custom_id", "?") if isinstance(data, dict) else "?"
    kind = "form" if interaction.type is discord.InteractionType.modal_submit else "button"
    return f"{kind} {custom_id}"


def _elapsed_ms(interaction: discord.Interaction) -> str:
    started = interaction.extras.get(_STARTED)
    return f" {round((time.monotonic() - started) * 1000)}ms" if started is not None else ""


def _line(interaction: discord.Interaction, outcome: str, detail: str = "") -> str:
    options = options_of(interaction)
    parts = [
        describe(interaction),
        f"by {who(interaction)}",
        f"in {where(interaction)}",
    ]
    if options:
        parts.append(f"[{options}]")
    parts.append(f"-> {outcome}{_elapsed_ms(interaction)}")
    if detail:
        parts.append(f"({detail})")
    return " ".join(parts)


@_never_raises
def log_received(interaction: discord.Interaction) -> None:
    """Buttons and forms have no completion event, so they are logged when they arrive."""
    mark_received(interaction)
    if interaction.type is discord.InteractionType.application_command:
        return
    if interaction.type not in (
        discord.InteractionType.component,
        discord.InteractionType.modal_submit,
    ):
        return  # autocomplete and pings: too noisy to be useful
    message = interaction.message
    suffix = f" on message {message.id}" if message else ""
    log.info(
        "%s by %s in %s%s", describe(interaction), who(interaction), where(interaction), suffix
    )


@_never_raises
def log_refusal(interaction: discord.Interaction, exc: BaseException) -> None:
    """The user asked for something the rules do not allow; the command replied with the reason.

    A slash command reports it in its completion line; a button has none, so it is logged here.
    """
    interaction.extras[_REFUSED] = str(exc)
    if interaction.type is not discord.InteractionType.application_command:
        log.info("%s by %s refused: %s", describe(interaction), who(interaction), exc)


@_never_raises
def log_completed(interaction: discord.Interaction) -> None:
    refused = interaction.extras.get(_REFUSED)
    if refused is not None:
        log.info(_line(interaction, "refused", str(refused)))
    else:
        log.info(_line(interaction, "ok"))


@_never_raises
def log_failed(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CheckFailure):
        log.warning(_line(interaction, "denied", type(error).__name__))
    else:
        log.error(_line(interaction, "error", type(error).__name__), exc_info=error)
