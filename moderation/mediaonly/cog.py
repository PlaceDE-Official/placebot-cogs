import re
from datetime import datetime
from typing import Optional

from aiohttp import ClientError, ClientSession
from discord import Embed, Forbidden, Guild, Message
from discord.ext import commands
from discord.ext.commands import CommandError, Context, UserInputError, guild_only

from PyDrocsid.cog import Cog
from PyDrocsid.command import docs, reply
from PyDrocsid.database import db, filter_by
from PyDrocsid.embeds import send_long_embed
from PyDrocsid.emojis import emoji_to_name
from PyDrocsid.events import StopEventHandling
from PyDrocsid.translations import t
from PyDrocsid.types import GuildMessageable

from .colors import Colors
from .models import MediaOnlyChannel, MediaOnlyDeletion
from .permissions import MediaOnlyPermission
from ...contributor import Contributor
from ...pubsub import can_respond_on_reaction, get_userlog_entries, send_alert, send_to_changelog


tg = t.g
t = t.mediaonly
emojis = list(sorted(set(map(re.escape, emoji_to_name)), key=lambda x: len(x), reverse=True))
not_rendering_markdown_chars = ["```", "``", "`", ("<", ">")]


def create_regex(chars, string) -> str:
    if isinstance(chars, tuple):
        return "(" + re.escape(chars[0]) + "[\\s\\S]*" + re.escape(string) + "[\\s\\S]*" + re.escape(chars[1]) + ")"
    return "(" + re.escape(chars) + "[\\s\\S]*" + re.escape(string) + "[\\s\\S]*" + re.escape(chars) + ")"


async def find_images(message: Message) -> list[str]:
    urls = [att.url for att in message.attachments]
    for link in re.finditer(r"(https?://([a-zA-Z0-9\-_~]+\.)+[a-zA-Z0-9\-_~]+(/\S*)?)", message.content):
        found = True
        for chars in not_rendering_markdown_chars:
            for match in re.finditer(create_regex(chars, link.group(1)), message.content):
                if match.start() <= link.start() and link.end() <= match.end():
                    found = False
                    break
        if found:
            urls.append(link.group(1))

    out = []
    for url in urls:
        # tenor only sends html, but discord displays it as gif
        if url.startswith("https://tenor.com/view/"):
            out.append(url)
            continue
        try:
            async with ClientSession() as session, session.head(url, allow_redirects=True) as response:
                content_length = int(response.headers.get("Content-length") or 256)
                mime = response.headers.get("Content-type") or ""
        except (KeyError, AttributeError, UnicodeError, ConnectionError, ClientError) as e:
            continue
        if (
            mime.lower() in ["image/gif", "image/gifv", "image/png", "image/jpg", "image/jpeg"]
            and content_length >= 256
        ):
            out.append(url)

    for sticker in message.stickers:
        out.append(sticker.url)
    return out


async def delete_message(message: Message, log: bool, mode: int, forbidden: bool, limit: int):
    try:
        await message.delete()
    except Forbidden:
        deleted = False
    else:
        deleted = True

    if log:
        await MediaOnlyDeletion.create(message.author.id, str(message.author), message.channel.id)

        description = t.deleted.character_limit(limit)
        if forbidden:
            description = t.deleted[mode]
        embed = Embed(title=t.mediaonly, description=description, colour=Colors.error)
        await message.channel.send(content=message.author.mention, embed=embed, delete_after=30)

        if deleted:
            await send_alert(message.guild, t.log_deleted_nomedia(message.author.mention, message.channel.mention))
        else:
            await send_alert(message.guild, t.log_nomedia_not_deleted(message.author.mention, message.channel.mention))


async def split_message(message: Message) -> [int, int, int]:
    urls = await find_images(message)
    remaining_text = message.content
    for url in urls:
        remaining_text = remaining_text.replace(url, "")
    emote_count = 0
    for emoji in emojis:
        remaining_text, count = re.subn(emoji, "", remaining_text, re.UNICODE)
        emote_count += count
    remaining_text, count = re.subn("<a?:.+?:\d+?>", "", remaining_text, re.UNICODE)
    emote_count += count
    remaining_text = remaining_text.strip()
    return len(urls), emote_count, len(remaining_text)


async def check_message(message: Message):
    if message.guild is None or message.author.bot:
        return
    if await MediaOnlyPermission.bypass.check_permissions(message.author):
        return
    if not (media_only := await MediaOnlyChannel.get(message.channel.id))[0]:
        return

    mode = media_only[1]
    max_length = int(media_only[2])
    log = bool(media_only[3])

    images, emotes, text = await split_message(message)

    # store mode id, if max_length is exceeded; if forbidden content
    for m, f_limit, f_forbidden in [
        (1, lambda: 0 < max_length < images, lambda: emotes > 0 or text > 0),
        (2, lambda: 0 < max_length < emotes, lambda: images > 0 or text > 0),
        (4, lambda: 0 < max_length < text, lambda: images > 0 or emotes > 0),
        (3, lambda: 0 < max_length < emotes + images, lambda: text > 0),
        (5, lambda: 0 < max_length < text + images, lambda: emotes > 0),
        (6, lambda: 0 < max_length < text + emotes, lambda: images > 0),
        (7, lambda: 0 < max_length < text + emotes + images, lambda: False),
    ]:
        if mode == m:
            fo = f_forbidden()
            if f_limit() or fo:
                await delete_message(message, log, mode, fo, max_length)
                raise StopEventHandling


class MediaOnlyCog(Cog, name="MediaOnly"):
    CONTRIBUTORS = [Contributor.Defelo, Contributor.wolflu]

    @can_respond_on_reaction.subscribe
    async def handle_can_respond_on_reaction(self, channel: GuildMessageable) -> bool:
        return not await db.exists(filter_by(MediaOnlyChannel, channel=channel.id))

    @get_userlog_entries.subscribe
    async def handle_get_userlog_entries(self, user_id: int, _) -> list[tuple[datetime, str]]:
        out: list[tuple[datetime, str]] = []

        deletion: MediaOnlyDeletion
        async for deletion in await db.stream(filter_by(MediaOnlyDeletion, member=user_id)):
            out.append((deletion.timestamp, t.ulog_deletion(f"<#{deletion.channel}>")))

        return out

    async def on_message(self, message: Message):
        await check_message(message)

    async def on_message_edit(self, _, after: Message):
        await check_message(after)

    @commands.group(aliases=["mo"])
    @MediaOnlyPermission.read.check
    @guild_only()
    @docs(t.commands.mediaonly)
    async def mediaonly(self, ctx: Context):
        if ctx.subcommand_passed is not None:
            if ctx.invoked_subcommand is None:
                raise UserInputError
            return

        guild: Guild = ctx.guild
        out = []
        async for channel in MediaOnlyChannel.stream():
            text_channel: Optional[GuildMessageable] = guild.get_channel(channel.channel)
            if not text_channel:
                await MediaOnlyChannel.remove(channel.channel)
                continue

            out.append(
                f":small_orange_diamond: {text_channel.mention};`{channel.mode}`;`{channel.max_length}`;`{channel.log}`"
            )

        embed = Embed(title=t.media_only_channels_header, colour=Colors.error)
        if out:
            out.append("")
            out.append("")
            out.append(t.syntax_explanation)
            out.append(t.mode_explanation)

            embed.colour = Colors.MediaOnly
            embed.description = "\n".join(out)
            await send_long_embed(ctx, embed)
        else:
            embed.description = t.no_media_only_channels
            await reply(ctx, embed=embed)

    @mediaonly.command(name="add", aliases=["a", "+"])
    @MediaOnlyPermission.write.check
    @docs(f"{t.commands.add}\n{t.mode_explanation}")
    async def mediaonly_add(
        self, ctx: Context, channel: GuildMessageable, mode: int, max_length: int | None = 0, log: bool = False
    ):
        if not 1 <= mode <= 7:
            raise CommandError(t.mode_invalid)
        if max_length < 0:
            raise CommandError(t.max_length_negative)

        if (await MediaOnlyChannel.get(channel.id))[0]:
            raise CommandError(t.channel_already_media_only)
        if not channel.permissions_for(channel.guild.me).manage_messages:
            raise CommandError(t.media_only_not_changed_no_permissions)

        await MediaOnlyChannel.add(channel.id, mode, max_length, log)
        embed = Embed(title=t.media_only_channels_header, description=t.channel_now_media_only, colour=Colors.MediaOnly)
        await reply(ctx, embed=embed)
        await send_to_changelog(ctx.guild, t.log_channel_now_media_only(channel.mention, mode, max_length, log))

    @mediaonly.command(name="remove", aliases=["del", "r", "d", "-"])
    @MediaOnlyPermission.write.check
    @docs(t.commands.remove)
    async def mediaonly_remove(self, ctx: Context, channel: GuildMessageable):
        if not (await MediaOnlyChannel.get(channel.id))[0]:
            raise CommandError(t.channel_not_media_only)

        await MediaOnlyChannel.remove(channel.id)
        embed = Embed(
            title=t.media_only_channels_header, description=t.channel_not_media_only_anymore, colour=Colors.MediaOnly
        )
        await reply(ctx, embed=embed)
        await send_to_changelog(ctx.guild, t.log_channel_not_media_only_anymore(channel.mention))
