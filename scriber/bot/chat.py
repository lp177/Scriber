"""Meeting text chat: capture the meeting channel's messages, relay dashboard messages.

While a meeting is recorded, every message posted in its text channel (the
voice channel's own chat, where the bot posts everything about the meeting) is
stored in the ``meeting_chat`` table, with later edits and deletions applied.
The dashboard shows that chat interleaved with the live transcript and can post
into the channel through the bot, so someone who is not in the voice channel
can still follow the meeting and take part in it.

The bot's own messages are never captured (recording notices, summaries…):
a message sent from the dashboard is stored by :func:`send_from_dashboard`
itself, flagged ``via_web``, with the dashboard author's name.

Reading message text needs Discord's privileged *Message Content* intent. When
it is not enabled for the application, the bot still connects (see
``ScribBot.start``) and captured messages simply arrive without text.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import discord

from scriber import database

if TYPE_CHECKING:
    from scriber.bot.client import ScribBot
    from scriber.bot.session import MeetingSession

log = logging.getLogger(__name__)

#: Longest message accepted from the dashboard (Discord caps a message at
#: 2000 characters; the "**name** (via dashboard):" prefix needs some room).
MAX_MESSAGE_LENGTH = 1800
#: Longest author name accepted from the dashboard.
MAX_AUTHOR_LENGTH = 40

#: User-authored message types; joins, pins, thread notices… are not chat.
_CHAT_TYPES = (discord.MessageType.default, discord.MessageType.reply)


class ChatError(Exception):
    """A dashboard message could not be posted; ``status`` is the HTTP status to use."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _iso(moment: datetime | None) -> str:
    """ISO 8601 UTC timestamp (second precision) of a Discord datetime."""
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _attachments(message: discord.Message) -> list[dict[str, Any]]:
    """Name, URL and type of a message's attachments (Discord's URLs expire)."""
    return [
        {
            "filename": attachment.filename,
            "url": attachment.url,
            "content_type": attachment.content_type or "",
        }
        for attachment in message.attachments
    ]


def _session_for_channel(bot: "ScribBot", guild_id: int | None, channel_id: int) -> "MeetingSession | None":
    """The recording session whose text channel is ``channel_id``, if any."""
    if guild_id is None:
        return None
    session = bot.active_sessions.get(guild_id)
    if session is None or getattr(session.text_channel, "id", None) != channel_id:
        return None
    return session


async def on_message(bot: "ScribBot", message: discord.Message) -> None:
    """Store a message posted in the channel of a meeting being recorded."""
    try:
        if message.guild is None or message.type not in _CHAT_TYPES:
            return
        if bot.user is not None and message.author.id == bot.user.id:
            return  # our own posts; dashboard relays are stored when sent
        session = _session_for_channel(bot, message.guild.id, message.channel.id)
        if session is None:
            return
        database.add_chat_message(
            session.meeting_id,
            message_id=str(message.id),
            author_id=str(message.author.id),
            author_name=message.author.display_name,
            content=message.clean_content,
            attachments=_attachments(message),
            created_at=_iso(message.created_at),
        )
    except Exception:
        log.warning("Could not store a chat message.", exc_info=True)


async def on_raw_message_edit(bot: "ScribBot", payload: discord.RawMessageUpdateEvent) -> None:
    """Apply an edit to a captured message (unknown messages are ignored)."""
    try:
        message = payload.message
        if bot.user is not None and message.author.id == bot.user.id:
            return
        database.update_chat_message(
            str(payload.message_id),
            content=message.clean_content,
            attachments=_attachments(message),
            edited_at=_iso(message.edited_at),
        )
    except Exception:
        log.warning("Could not apply a chat message edit.", exc_info=True)


async def on_raw_message_delete(bot: "ScribBot", payload: discord.RawMessageDeleteEvent) -> None:
    """Mark a captured message as deleted (unknown messages are ignored)."""
    try:
        database.update_chat_message(str(payload.message_id), deleted=True)
    except Exception:
        log.warning("Could not apply a chat message deletion.", exc_info=True)


async def send_from_dashboard(
    session: "MeetingSession", author: str, content: str
) -> dict[str, Any]:
    """Post a dashboard message into the meeting channel and store it.

    The message is prefixed with the author's name so the channel knows who
    wrote it, and sent with every mention disabled: the dashboard must not be
    a way to ping ``@everyone`` or a role. Raises :class:`ChatError`.
    """
    author = " ".join(author.split())[:MAX_AUTHOR_LENGTH] or "Dashboard"
    content = content.strip()
    if not content:
        raise ChatError("The message is empty.")
    if len(content) > MAX_MESSAGE_LENGTH:
        raise ChatError(f"The message is too long (at most {MAX_MESSAGE_LENGTH} characters).")
    channel = session.text_channel
    if channel is None:
        raise ChatError("The meeting channel is not available.", status=409)
    text = f"**{discord.utils.escape_markdown(author)}** (via dashboard): {content}"
    try:
        sent = await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden as exc:
        raise ChatError(
            "Discord refused the message — the bot may lack the Send Messages "
            "permission in this channel.",
            status=502,
        ) from exc
    except discord.HTTPException as exc:
        raise ChatError(f"Discord rejected the message: {exc}", status=502) from exc
    return database.add_chat_message(
        session.meeting_id,
        message_id=str(sent.id),
        author_name=author,
        content=content,
        created_at=_iso(sent.created_at),
        via_web=True,
    )
