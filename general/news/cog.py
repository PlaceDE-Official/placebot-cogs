import re
from typing import Dict, List, Optional, Union

from discord import AllowedMentions, Embed, Forbidden, HTTPException, Member, Role, TextChannel
from discord.ext import commands
from discord.ext.commands import CommandError, Context, UserInputError, guild_only
from sqlalchemy import and_

from PyDrocsid.cog import Cog
from PyDrocsid.command import add_reactions, optional_permissions, reply
from PyDrocsid.database import db, select
from PyDrocsid.discohook import DiscoHookError, MessageContent, load_discohook_link
from PyDrocsid.embeds import send_long_embed, split_message
from PyDrocsid.translations import t
from PyDrocsid.util import ZERO_WIDTH_WHITESPACE, RoleListConverter, attachment_to_file, check_message_send_permissions

from .colors import Colors
from .models import NewsAuthorization
from .permissions import NewsPermission
from ...contributor import Contributor
from ...pubsub import send_to_changelog


tg = t.g
t = t.news


async def list_auth(ctx: Context, member: Optional[Member] = None):
    embed = Embed(title=t.news, colour=Colors.News)
    channels: Dict[TextChannel, Dict[Union[Member, Role], List[Role]]] = {}
    auth: NewsAuthorization
    if member:
        authorizations = db.stream(
            select(NewsAuthorization).filter(
                NewsAuthorization.source_id.in_([role.id for role in member.roles] + [member.id])
            )
        )
    else:
        authorizations = db.stream(select(NewsAuthorization))
    async for auth in await authorizations:
        source: Optional[Union[Member, Role]] = ctx.guild.get_member(auth.source_id) or ctx.guild.get_role(
            auth.source_id
        )
        notification_rid: Optional[Role] = ctx.guild.get_role(auth.notification_role_id)
        channel: Optional[TextChannel] = ctx.guild.get_channel(auth.channel_id)
        if source is None or channel is None or notification_rid is None and auth.notification_role_id is not None:
            await db.delete(auth)
            continue
        lst = channels.setdefault(channel, {}).setdefault(source, [])
        if notification_rid:
            lst.append(notification_rid)

    if not channels:
        embed.description = t.no_news_authorizations if not member else t.single_no_news_authorizations
        embed.colour = Colors.error
        await reply(ctx, embed=embed)
        return

    def make_field(auths: Dict[Union[Member, Role], List[Role]]) -> List[str]:
        out = []
        for src, targets in sorted(auths.items(), key=lambda a: (isinstance(a[0], Role), a[0].name)):
            line = f":small_orange_diamond: {src.mention}"
            if targets:
                line += " -> " + ", ".join(target.mention for target in targets)
            out.append(line)
        return out

    for channel in sorted(channels, key=lambda x: x.name):
        embed.add_field(name=channel.mention, value="\n".join(make_field(channels[channel])), inline=False)
    await send_long_embed(ctx.message, embed)


class NewsCog(Cog, name="News"):
    CONTRIBUTORS = [Contributor.Defelo, Contributor.wolflu, Contributor.TNT2k]

    @commands.group()
    @guild_only()
    async def news(self, ctx: Context):
        """
        manage news channels
        """

        if ctx.invoked_subcommand is None:
            raise UserInputError

    @news.group(name="auth", aliases=["a"])
    @optional_permissions(NewsPermission.read)
    async def news_auth(self, ctx: Context):
        """
        manage authorized users and channels
        """
        if len(ctx.message.content.lstrip(ctx.prefix).split()) > 2:
            if ctx.invoked_subcommand is None:
                raise UserInputError
            return

        if not await NewsPermission.read.check_permissions(ctx.author):
            raise CommandError(tg.not_allowed)
        await list_auth(ctx)

    @news_auth.command(name="own")
    @NewsPermission.view_own.check
    async def news_auth_own(self, ctx: Context):
        await list_auth(ctx, ctx.author)

    @news_auth.command(name="other")
    @NewsPermission.view_other.check
    async def news_auth_other(self, ctx: Context, member: Member):
        await list_auth(ctx, member)

    @news_auth.command(name="add", aliases=["a", "+"])
    @NewsPermission.write.check
    async def news_auth_add(
        self,
        ctx: Context,
        source: Union[Member, Role],
        channel: TextChannel,
        *,
        allowed_roles: RoleListConverter = None,
    ):
        """
        authorize a new user / role to send news to a specific channel (and optionally ping specific roles)
        This is "additive"; you can add more roles later without needing to specify all allowed roles every time
        """
        if not allowed_roles:
            allowed_roles = []
        # get source as translation string
        source_type = t.user if isinstance(source, Member) else t.role
        allowed_roles: List[Role]
        # if we can not send messages to this channel, abort
        if not channel.permissions_for(channel.guild.me).send_messages:
            raise CommandError(t.news_not_added_no_permissions)

        # if we do not allow pings
        if not allowed_roles:
            if await db.exists(select(NewsAuthorization).filter_by(source_id=source.id, channel_id=channel.id)):
                raise CommandError(t.news_already_authorized(source_type))
            await NewsAuthorization.create(source.id, channel.id, None)
            embed = Embed(title=t.news, colour=Colors.News, description=t.news_authorized(source_type))
            await reply(ctx, embed=embed)
            await send_to_changelog(ctx.guild, t.log_news_authorized(source_type, source.mention, channel.mention))
            return

        missing_roles = []
        # check if any of the allowed pings are new
        for role in allowed_roles:
            if not await db.exists(
                select(NewsAuthorization).filter_by(
                    source_id=source.id, channel_id=channel.id, notification_role_id=role.id
                )
            ):
                missing_roles.append(role)

        # if not, abort
        if not missing_roles:
            raise CommandError(t.news_already_authorized_ping(source_type))

        # create missing auths
        for role in missing_roles:
            await NewsAuthorization.create(source.id, channel.id, role.id)

        *roles, last = (x.mention for x in missing_roles)
        roles: list[str]
        value1 = t.news_authorized_ping(target_type=source_type, roles=", ".join(roles), last=last, cnt=len(roles) + 1)
        embed = Embed(title=t.news, colour=Colors.News, description=value1)
        await reply(ctx, embed=embed)
        value2 = t.log_news_authorized_ping(
            source.mention,
            channel.mention,
            target_type=source_type,
            roles=", ".join(roles),
            last=last,
            cnt=len(roles) + 1,
        )
        await send_to_changelog(ctx.guild, value2)

    @news_auth.command(name="remove", aliases=["del", "r", "d", "-"])
    @NewsPermission.write.check
    async def news_auth_remove(
        self,
        ctx: Context,
        source: Union[Member, Role],
        channel: TextChannel,
        *,
        allowed_roles: Optional[RoleListConverter],
    ):
        """
        remove an authorization for a user / role to send news to a specific channel
        This is "subtractive"; you can remove roles without needing to specify all allowed roles every time
        If you omit `allowed_pings`, all authorizations for this user / role for this channel will be deleted
        Otherwise, only authorizations for matching `allowed_roles` will be removed
        """
        if not allowed_roles:
            allowed_roles = []
        allowed_roles: List[Role]
        source_type = t.user if isinstance(source, Member) else t.role
        if not allowed_roles:
            authorization: List[NewsAuthorization] = await db.all(
                select(NewsAuthorization).filter_by(source_id=source.id, channel_id=channel.id)
            )
            if not authorization:
                raise CommandError(t.news_not_authorized)
            for auth in authorization:
                await db.delete(auth)
            embed = Embed(title=t.news, colour=Colors.News, description=t.news_unauthorized(source_type))
            await reply(ctx, embed=embed)
            await send_to_changelog(ctx.guild, t.log_news_unauthorized(source_type, source.mention, channel.mention))
            return

        deleted = []
        for ping in allowed_roles:
            authorization: Optional[NewsAuthorization] = await db.first(
                select(NewsAuthorization).filter_by(
                    source_id=source.id, channel_id=channel.id, notification_role_id=ping.id
                )
            )
            if authorization:
                deleted.append(ping)
                await db.delete(authorization)
        if not deleted:
            raise CommandError(t.nothing_to_delete_ping(source_type))

        *roles, last = (x.mention for x in deleted)
        roles: list[str]
        value1 = t.news_unauthorized_ping(
            target_type=source_type, roles=", ".join(roles), last=last, cnt=len(roles) + 1
        )
        embed = Embed(title=t.news, colour=Colors.News, description=value1)
        await reply(ctx, embed=embed)
        value2 = t.log_news_unauthorized_ping(
            source.mention,
            channel.mention,
            target_type=source_type,
            roles=", ".join(roles),
            last=last,
            cnt=len(roles) + 1,
        )
        await send_to_changelog(ctx.guild, value2)

    @news.command(name="send", aliases=["s"])
    async def news_send(self, ctx: Context, channel: TextChannel, *, discohook_url: str):
        """
        send a news message
        - generate the discohook link using https://discohook.org (use "Share Message" at the top of the page to get short link)
        - add attachments to this command (not the message on discohook) to attach them to the sent message

        the `<>` below are part of the pings, do not remove them!
        - to ping using discohook (works in discord as well), use the templates below
        - roles: `<@&ROLE_ID>`
        - channels: `<#CHANNEL_ID>`
        - users: `<@!USER_ID>`
        - if you want to create a notification, the ping needs to be in a normal message, not within an embed!
        - you can create pings in embeds, if you do not want to notify anyone

        @everyone can only be pinged using the role id of the default role (it is the same id as the guild id).
        @here can not be pinged at all.
        """

        authorizations: list[NewsAuthorization] = await db.all(
            select(NewsAuthorization).filter(
                and_(
                    NewsAuthorization.source_id.in_([role.id for role in ctx.author.roles] + [ctx.author.id]),
                    NewsAuthorization.channel_id == channel.id,
                )
            )
        )

        if not authorizations:
            raise CommandError(t.news_you_are_not_authorized_channel)

        try:
            messages: list[MessageContent] = [
                msg for msg in await load_discohook_link(discohook_url) if not msg.is_empty
            ]
        except DiscoHookError:
            raise CommandError(t.discohook_invalid)

        if not messages:
            raise CommandError(t.discohook_empty)

        pings: dict[int, Role] = {}
        for message in messages:
            for match in re.findall(r"<@&(\d*?)>", message.content):
                if match.isnumeric() and (role := ctx.guild.get_role(int(match))):
                    pings.update({role.id: role})
        for auth in authorizations:
            if auth.notification_role_id:
                pings.pop(auth.notification_role_id, None)
        if pings:
            *roles, last = (x.mention for x in pings.values())
            raise CommandError(
                t.news_you_are_not_authorized_ping(roles=", ".join(roles), last=last, cnt=len(roles) + 1)
            )

        check_message_send_permissions(channel, check_embed=any(m.embeds for m in messages))

        try:
            for message in messages:
                for msg in split_message(message.embeds, message.content):
                    content: str | None = msg[0]
                    if content:
                        content = content.replace("@everyone", f"@{ZERO_WIDTH_WHITESPACE}everyone").replace(
                            "@here", f"@{ZERO_WIDTH_WHITESPACE}here"
                        )
                    await ctx.author.send(content=content, embeds=msg[1])
            if ctx.message.attachments:
                await channel.send(
                    files=[await attachment_to_file(attachment) for attachment in ctx.message.attachments]
                )
        except (HTTPException, Forbidden) as e:
            if e.status == 400 and "Invalid Form" in e.text:
                raise CommandError(t.message_not_compliant)
            raise CommandError(t.msg_could_not_be_sent)

        await add_reactions(ctx.message, "white_check_mark")

    @news.command(name="test", aliases=["t"])
    async def news_test(self, ctx: Context, *, discohook_url: str):
        """
        tests a news message (replies in the channel in which the command was sent, no one is pinged)
        - generate the discohook link using https://discohook.org (use "Share Message" at the top of the page to get short link)
        - add attachments to this command (not the message on discohook) to attach them to the sent message

        the `<>` below are part of the pings, do not remove them!
        - to ping using discohook (works in discord as well), use the templates below
        - roles: `<@&ROLE_ID>`
        - channels: `<#CHANNEL_ID>`
        - users: `<@!USER_ID>`
        - if you want to create a notification, the ping needs to be in a normal message, not within an embed!
        - you can create pings in embeds, if you do not want to notify anyone
        """

        authorizations: list[NewsAuthorization] = await db.all(
            select(NewsAuthorization).filter(
                NewsAuthorization.source_id.in_([role.id for role in ctx.author.roles] + [ctx.author.id])
            )
        )

        if not authorizations:
            raise CommandError(t.news_you_are_not_authorized)

        try:
            messages: list[MessageContent] = [
                msg for msg in await load_discohook_link(discohook_url) if not msg.is_empty
            ]
        except DiscoHookError:
            raise CommandError(t.discohook_invalid)

        if not messages:
            raise CommandError(t.discohook_empty)

        check_message_send_permissions(ctx.channel, check_embed=any(m.embeds for m in messages))

        try:
            for message in messages:
                for msg in split_message(message.embeds, message.content):
                    await ctx.author.send(content=msg[0], embeds=msg[1])
            if ctx.message.attachments:
                await ctx.author.send(
                    files=[await attachment_to_file(attachment) for attachment in ctx.message.attachments],
                    allowed_mentions=AllowedMentions.none(),
                )
        except (HTTPException, Forbidden) as e:
            if e.status == 400 and "Invalid Form" in e.text:
                raise CommandError(t.message_not_compliant)
            raise CommandError(t.msg_could_not_be_sent_dm)

        await add_reactions(ctx.message, "white_check_mark")
