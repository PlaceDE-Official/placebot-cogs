import re
from typing import Dict, List, Optional, Union

from discord import Embed, Forbidden, HTTPException, Member, Option, Role, SlashCommandGroup, TextChannel
from discord.commands import ApplicationContext
from discord.ext.commands import CommandError, Context
from sqlalchemy import and_

from PyDrocsid.cog import Cog
from PyDrocsid.command import optional_permissions, reply
from PyDrocsid.database import db, select
from PyDrocsid.discohook import DiscoHookError, MessageContent, load_discohook_link
from PyDrocsid.embeds import send_long_embed, split_message
from PyDrocsid.translations import t
from PyDrocsid.types import GuildMessageable
from PyDrocsid.util import ZERO_WIDTH_WHITESPACE, RoleListConverter, check_message_send_permissions

from .colors import Colors
from .models import NewsAuthorization
from .permissions import NewsPermission
from ...contributor import Contributor
from ...pubsub import send_to_changelog


tg = t.g
t = t.news


async def list_auth(ctx: Context, member: Optional[Member] = None):
    embed = Embed(title=t.news, colour=Colors.News)
    channels: Dict[GuildMessageable, Dict[Union[Member, Role], List[Role]]] = {}
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
        channel: Optional[GuildMessageable] = ctx.guild.get_channel(auth.channel_id)
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
    await send_long_embed(ctx, embed)


class NewsCog(Cog, name="News"):
    CONTRIBUTORS = [Contributor.Defelo, Contributor.wolflu, Contributor.TNT2k]

    news = SlashCommandGroup(
        name="news",
        description="News-Kanäle und -Berechtigungen verwalten",
        guild_only=True,
    )

    auth = news.create_subgroup(
        name="auth",
        description="Autorisierungen verwalten",
    )

    @staticmethod
    def resolve_source(guild, source_str: str) -> Union[Member, Role]:
        clean = re.sub(r"[<@!&>]", "", source_str).strip()
        if clean.isdigit():
            target_id = int(clean)
            if member := guild.get_member(target_id):
                return member
            if role := guild.get_role(target_id):
                return role
        raise CommandError("Mitglied oder Rolle nicht gefunden!")

    @auth.command(name="list", description="Alle News-Autorisierungen auflisten")
    @optional_permissions(NewsPermission.read)
    async def news_auth_list(self, ctx: ApplicationContext):
        if not await NewsPermission.read.check_permissions(ctx.author):
            raise CommandError(tg.not_allowed)
        await list_auth(ctx)

    @auth.command(name="own", description="Eigene News-Autorisierungen auflisten")
    @NewsPermission.view_own.check
    async def news_auth_own(self, ctx: ApplicationContext):
        await list_auth(ctx, ctx.author)

    @auth.command(name="other", description="Autorisierungen eines bestimmten Mitglieds anzeigen")
    @NewsPermission.view_other.check
    async def news_auth_other(self, ctx: ApplicationContext, member: Member = Option(Member, description="Mitglied")):
        await list_auth(ctx, member)

    @auth.command(name="add", description="Autorisierung für Nutzer oder Rolle hinzufügen")
    @NewsPermission.write.check
    async def news_auth_add(
        self,
        ctx: ApplicationContext,
        source: str = Option(str, description="Mitglied oder Rolle (Mention oder ID)"),
        channel: TextChannel = Option(TextChannel, description="Zielkanal"),
        allowed_roles: str = Option(str, description="Erlaubte Ping-Rollen (Mentions/IDs, getrennt durch Leerzeichen)", required=False, default=""),
    ):
        """
        authorize a new user / role to send news to a specific channel (and optionally ping specific roles)
        """
        source_obj = self.resolve_source(ctx.guild, source)
        parsed_roles: list[Role] = []
        if allowed_roles and allowed_roles.strip():
            parsed_roles = await RoleListConverter().convert(ctx, allowed_roles.strip())

        source_type = t.user if isinstance(source_obj, Member) else t.role
        if not channel.permissions_for(channel.guild.me).send_messages:
            raise CommandError(t.news_not_added_no_permissions)

        if not parsed_roles:
            if await db.exists(select(NewsAuthorization).filter_by(source_id=source_obj.id, channel_id=channel.id)):
                raise CommandError(t.news_already_authorized(source_type))
            await NewsAuthorization.create(source_obj.id, channel.id, None)
            embed = Embed(title=t.news, colour=Colors.News, description=t.news_authorized(source_type))
            await reply(ctx, embed=embed)
            await send_to_changelog(ctx.guild, t.log_news_authorized(source_type, source_obj.mention, channel.mention))
            return

        missing_roles = []
        for role in parsed_roles:
            if not await db.exists(
                select(NewsAuthorization).filter_by(
                    source_id=source_obj.id, channel_id=channel.id, notification_role_id=role.id
                )
            ):
                missing_roles.append(role)

        if not missing_roles:
            raise CommandError(t.news_already_authorized_ping(source_type))

        for role in missing_roles:
            await NewsAuthorization.create(source_obj.id, channel.id, role.id)

        *roles, last = (x.mention for x in missing_roles)
        roles: list[str]
        value1 = t.news_authorized_ping(target_type=source_type, roles=", ".join(roles), last=last, cnt=len(roles) + 1)
        embed = Embed(title=t.news, colour=Colors.News, description=value1)
        await reply(ctx, embed=embed)
        value2 = t.log_news_authorized_ping(
            source_obj.mention,
            channel.mention,
            target_type=source_type,
            roles=", ".join(roles),
            last=last,
            cnt=len(roles) + 1,
        )
        await send_to_changelog(ctx.guild, value2)

    @auth.command(name="remove", description="Autorisierung für Nutzer oder Rolle entfernen")
    @NewsPermission.write.check
    async def news_auth_remove(
        self,
        ctx: ApplicationContext,
        source: str = Option(str, description="Mitglied oder Rolle (Mention oder ID)"),
        channel: TextChannel = Option(TextChannel, description="Zielkanal"),
        allowed_roles: str = Option(str, description="Zu entfernende Ping-Rollen (Mentions/IDs; leer = alles löschen)", required=False, default=""),
    ):
        source_obj = self.resolve_source(ctx.guild, source)
        parsed_roles: list[Role] = []
        if allowed_roles and allowed_roles.strip():
            parsed_roles = await RoleListConverter().convert(ctx, allowed_roles.strip())

        source_type = t.user if isinstance(source_obj, Member) else t.role
        if not parsed_roles:
            authorization: List[NewsAuthorization] = await db.all(
                select(NewsAuthorization).filter_by(source_id=source_obj.id, channel_id=channel.id)
            )
            if not authorization:
                raise CommandError(t.news_not_authorized)
            for auth in authorization:
                await db.delete(auth)
            embed = Embed(title=t.news, colour=Colors.News, description=t.news_unauthorized(source_type))
            await reply(ctx, embed=embed)
            await send_to_changelog(ctx.guild, t.log_news_unauthorized(source_type, source_obj.mention, channel.mention))
            return

        deleted = []
        for ping in parsed_roles:
            authorization: Optional[NewsAuthorization] = await db.first(
                select(NewsAuthorization).filter_by(
                    source_id=source_obj.id, channel_id=channel.id, notification_role_id=ping.id
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
            source_obj.mention,
            channel.mention,
            target_type=source_type,
            roles=", ".join(roles),
            last=last,
            cnt=len(roles) + 1,
        )
        await send_to_changelog(ctx.guild, value2)

    @news.command(name="send", description="News-Nachricht via Discohook-Link in einen Kanal senden")
    async def news_send(
        self,
        ctx: ApplicationContext,
        channel: TextChannel = Option(TextChannel, description="Zielkanal"),
        discohook_url: str = Option(str, description="Discohook-URL"),
    ):
        await ctx.defer(ephemeral=True)
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
                    await channel.send(content=content, embeds=msg[1])
        except (HTTPException, Forbidden) as e:
            if e.status == 400 and "Invalid Form" in e.text:
                raise CommandError(t.message_not_compliant)
            raise CommandError(t.msg_could_not_be_sent)

        await reply(ctx, content=":white_check_mark: News-Nachricht erfolgreich gesendet!")

    @news.command(name="test", description="News-Nachricht per DM testen")
    async def news_test(self, ctx: ApplicationContext, discohook_url: str = Option(str, description="Discohook-URL")):
        await ctx.defer(ephemeral=True)
        try:
            messages: list[MessageContent] = [
                msg for msg in await load_discohook_link(discohook_url) if not msg.is_empty
            ]
        except DiscoHookError:
            raise CommandError(t.discohook_invalid)

        if not messages:
            raise CommandError(t.discohook_empty)

        try:
            for message in messages:
                for msg in split_message(message.embeds, message.content):
                    await ctx.author.send(content=msg[0], embeds=msg[1])
        except (HTTPException, Forbidden) as e:
            if e.status == 400 and "Invalid Form" in e.text:
                raise CommandError(t.message_not_compliant)
            raise CommandError(t.msg_could_not_be_sent_dm)

        await reply(ctx, content=":white_check_mark: Testnachricht per DM gesendet!")
