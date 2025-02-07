from typing import Optional

from discord import Embed, File, Forbidden, HTTPException, Member, Message, NotFound, Permissions
from discord.ext import commands
from discord.ext.commands import CommandError, Context, UserInputError, guild_only

from PyDrocsid.cog import Cog
from PyDrocsid.command import Confirmation, add_reactions, docs, reply
from PyDrocsid.converter import Color
from PyDrocsid.discohook import (
    DISCOHOOK_EMPTY_MESSAGE,
    DiscoHookError,
    MessageContent,
    create_discohook_link,
    load_discohook_link,
)
from PyDrocsid.embeds import split_message
from PyDrocsid.translations import t
from PyDrocsid.types import GuildMessageable
from PyDrocsid.util import check_message_send_permissions, read_complete_message, read_normal_message

from .colors import Colors
from .permissions import MessagePermission
from ...contributor import Contributor
from ...pubsub import send_alert


tg = t.g
t = t.message


class MessageCog(Cog, name="Message Commands"):
    CONTRIBUTORS = [Contributor.Defelo, Contributor.wolflu, Contributor.LoC]

    async def get_message_cancel(self, channel: GuildMessageable, member: Member) -> tuple[Optional[str], list[File]]:
        content, files = await read_normal_message(self.bot, channel, member)
        if content == t.cancel:
            embed = Embed(title=t.messages, colour=Colors.MessageCommands, description=t.msg_send_cancel)
            await channel.send(embed=embed)
            return None, []

        return content, files

    @commands.group()
    @MessagePermission.send.check
    @guild_only()
    @docs(t.commands.send)
    async def send(self, ctx: Context):
        if ctx.invoked_subcommand is None:
            raise UserInputError

    @send.command(name="text", aliases=["t"])
    @docs(t.commands.send_text)
    async def send_text(self, ctx: Context, channel: GuildMessageable):
        check_message_send_permissions(channel)

        embed = Embed(title=t.messages, colour=Colors.MessageCommands, description=t.send_message(t.cancel))
        await reply(ctx, embed=embed)
        content, files = await self.get_message_cancel(ctx.channel, ctx.author)

        if content is None:
            return

        try:
            await channel.send(content=content, files=files)
        except (HTTPException, Forbidden):
            raise CommandError(t.msg_could_not_be_sent)
        else:
            embed.description = t.msg_sent
            await reply(ctx, embed=embed)

    @send.command(name="embed", aliases=["e"])
    @docs(t.commands.send_embed)
    async def send_embed(self, ctx: Context, channel: GuildMessageable, color: Optional[Color] = None):
        check_message_send_permissions(channel, check_embed=True)

        embed = Embed(title=t.messages, colour=Colors.MessageCommands, description=t.send_embed_title(t.cancel))
        await reply(ctx, embed=embed)
        title, _ = await self.get_message_cancel(ctx.channel, ctx.author)
        if title is None:
            return
        if len(title) > 256:
            raise CommandError(t.title_too_long)

        embed.description = t.send_embed_content(t.cancel)
        await reply(ctx, embed=embed)
        content, files = await self.get_message_cancel(ctx.channel, ctx.author)

        if content is None:
            return

        send_embed = Embed(title=title, description=content)

        if files and any(files[0].filename.lower().endswith(ext) for ext in ["jpg", "jpeg", "png", "gif"]):
            send_embed.set_image(url="attachment://" + files[0].filename)

        if color is not None:
            send_embed.colour = color

        try:
            await channel.send(embed=send_embed, files=files)
        except (HTTPException, Forbidden):
            raise CommandError(t.msg_could_not_be_sent)
        else:
            embed.description = t.msg_sent
            await reply(ctx, embed=embed)

    @send.command(name="copy", aliases=["c"])
    @docs(t.commands.send_copy)
    async def send_copy(self, ctx: Context, channel: GuildMessageable, message: Message):
        content, files, embed = await read_complete_message(message)
        try:
            await channel.send(content=content, embed=embed, files=files)
        except (HTTPException, Forbidden):
            raise CommandError(t.msg_could_not_be_sent)
        else:
            embed = Embed(title=t.messages, colour=Colors.MessageCommands, description=t.msg_sent)
            await reply(ctx, embed=embed)

    @send.command(name="discohook", aliases=["dh"])
    @docs(t.commands.send_discohook(DISCOHOOK_EMPTY_MESSAGE))
    async def send_discohook(self, ctx: Context, channel: GuildMessageable, *, discohook_url: str):
        try:
            messages: list[MessageContent] = [
                msg for msg in await load_discohook_link(discohook_url) if not msg.is_empty
            ]
        except DiscoHookError:
            raise CommandError(t.discohook_invalid)

        if not messages:
            raise CommandError(t.discohook_empty)

        check_message_send_permissions(channel, check_embed=any(m.embeds for m in messages))

        try:
            for message in messages:
                for msg in split_message(message.embeds, message.content):
                    await channel.send(content=msg[0], embeds=msg[1])
        except (HTTPException, Forbidden):
            raise CommandError(t.msg_could_not_be_sent)

        await add_reactions(ctx.message, "white_check_mark")

    @commands.group()
    @MessagePermission.edit.check
    @guild_only()
    @docs(t.commands.edit)
    async def edit(self, ctx: Context):
        if ctx.invoked_subcommand is None:
            raise UserInputError

    @edit.command(name="text", aliases=["t"])
    @docs(t.commands.edit_text)
    async def edit_text(self, ctx: Context, message: Message):
        if message.author != self.bot.user:
            raise CommandError(t.could_not_edit)
        check_message_send_permissions(message.channel, check_send=False)

        embed = Embed(title=t.messages, colour=Colors.MessageCommands, description=t.send_new_message(t.cancel))
        await reply(ctx, embed=embed)
        content, files = await self.get_message_cancel(ctx.channel, ctx.author)

        if content is None:
            return

        if files:
            raise CommandError(t.cannot_edit_files)

        await message.edit(content=content, embed=None)
        embed.description = t.msg_edited
        await reply(ctx, embed=embed)

    @edit.command(name="embed", aliases=["e"])
    @docs(t.commands.edit_embed)
    async def edit_embed(self, ctx: Context, message: Message, color: Optional[Color] = None):
        if message.author != self.bot.user:
            raise CommandError(t.could_not_edit)
        check_message_send_permissions(message.channel, check_send=False, check_embed=True)

        embed = Embed(title=t.messages, colour=Colors.MessageCommands, description=t.send_embed_title(t.cancel))
        await reply(ctx, embed=embed)
        title, _ = await self.get_message_cancel(ctx.channel, ctx.author)

        if title is None:
            return
        if len(title) > 256:
            raise CommandError(t.title_too_long)

        embed.description = t.send_embed_content(t.cancel)
        await reply(ctx, embed=embed)
        content, _ = await self.get_message_cancel(ctx.channel, ctx.author)

        if content is None:
            return

        send_embed = Embed(title=title, description=content)

        if color is not None:
            send_embed.colour = color
        elif message.embeds and message.embeds[0].color:
            send_embed.colour = message.embeds[0].color

        await message.edit(content=None, files=[], embed=send_embed)
        embed.description = t.msg_edited
        await reply(ctx, embed=embed)

    @edit.command(name="copy", aliases=["c"])
    @docs(t.commands.edit_copy)
    async def edit_copy(self, ctx: Context, message: Message, source: Message):
        if message.author != self.bot.user:
            raise CommandError(t.could_not_edit)

        content, files, embed = await read_complete_message(source)
        if files:
            raise CommandError(t.cannot_edit_files)
        await message.edit(content=content, embed=embed)
        embed = Embed(title=t.messages, colour=Colors.MessageCommands, description=t.msg_edited)
        await reply(ctx, embed=embed)

    @edit.command(name="discohook", aliases=["dh"])
    @docs(t.commands.edit_discohook(DISCOHOOK_EMPTY_MESSAGE))
    async def edit_discohook(self, ctx: Context, message: Message, discohook_url: str):
        if message.author != self.bot.user:
            raise CommandError(t.could_not_edit)

        try:
            messages: list[MessageContent] = [
                msg for msg in await load_discohook_link(discohook_url) if not msg.is_empty
            ]
        except DiscoHookError:
            raise CommandError(t.discohook_invalid)

        if not messages:
            raise CommandError(t.discohook_empty)
        if len(messages) > 1:
            raise CommandError(t.discohook_multiple_messages)

        content, embeds = messages[0]
        # why did this exist?
        #if len(embeds) > 1:
         #   raise CommandError(t.discohook_multiple_embeds)

        try:
            await message.edit(content=content, embeds=embeds)
        except (HTTPException, Forbidden):
            raise CommandError(t.msg_could_not_be_sent)

        await add_reactions(ctx.message, "white_check_mark")

    @commands.command()
    @MessagePermission.delete.check
    @guild_only()
    @docs(t.commands.delete)
    async def delete(self, ctx: Context, message: Message):
        if message.guild is None:
            raise CommandError(t.cannot_delete_dm)

        channel: GuildMessageable = message.channel
        permissions: Permissions = channel.permissions_for(message.guild.me)
        if message.author != self.bot.user and not permissions.manage_messages or message.is_system():
            raise CommandError(t.could_not_delete)

        await message.delete()
        embed = Embed(title=t.messages, colour=Colors.MessageCommands, description=t.msg_deleted)
        await reply(ctx, embed=embed)

    @commands.command(aliases=["clean"])
    @MessagePermission.clear.check
    @guild_only()
    @docs(t.commands.clear)
    async def clear(self, ctx: Context, count: int):
        channel: GuildMessageable = ctx.channel

        if count not in range(1, 101):
            raise CommandError(t.count_between)

        if not await Confirmation().run(ctx, t.confirm(channel.mention, cnt=count)):
            return

        messages = (await channel.history(limit=count + 2).flatten())[2:]
        try:
            await channel.delete_messages(messages)
        except (Forbidden, NotFound, HTTPException):
            raise CommandError(t.msg_not_deleted)

        await reply(
            ctx,
            embed=Embed(
                title=t.clear_channel,
                description=t.deleted_messages(channel.mention, cnt=count),
                color=Colors.MessageCommands,
            ),
        )
        await send_alert(ctx.guild, t.log_cleared(ctx.author.mention, channel.mention, cnt=count))

    @commands.command(aliases=["dh"])
    @docs(t.commands.discohook)
    async def discohook(self, ctx: Context, *messages: Message):
        if not messages:
            raise UserInputError
        for msg in messages:
            if not msg.channel.permissions_for(ctx.author).read_message_history:
                raise CommandError(t.cannot_read_messages(msg.channel.mention))

        try:
            url = await create_discohook_link(*messages)
        except DiscoHookError:
            raise CommandError(t.discohook_create_failed)

        await reply(ctx, url)
