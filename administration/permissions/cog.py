import asyncio
from typing import Optional

from discord import Embed, Option, Role, SlashCommandGroup
from discord.commands import ApplicationContext
from discord.ext.commands import BadArgument, CommandError, Context, Converter

from PyDrocsid.cog import Cog
from PyDrocsid.command import reply
from PyDrocsid.config import Config, get_subclasses_in_enabled_packages
from PyDrocsid.embeds import send_long_embed
from PyDrocsid.permission import BasePermission, BasePermissionLevel
from PyDrocsid.settings import RoleSettings
from PyDrocsid.translations import t
from PyDrocsid.redis_client import redis

from .colors import Colors
from .permissions import PermissionsPermission
from ...contributor import Contributor
from ...pubsub import send_to_changelog


tg = t.g
t = t.permissions


def get_permissions() -> list[BasePermission]:
    permissions: list[BasePermission] = []
    for cls in get_subclasses_in_enabled_packages(BasePermission):
        permissions += [x for x in cls if not hasattr(x, "_disabled")]
    return permissions


async def list_permissions(ctx: Context, title: str, min_level: BasePermissionLevel):
    out = {}
    permissions: list[BasePermission] = get_permissions()
    levels = await asyncio.gather(*[permission.resolve() for permission in permissions])
    for permission, level in zip(permissions, levels):
        if min_level.level >= level.level:
            out.setdefault((level.level, level.description), []).append(
                f"`{permission.fullname}` - {permission.description}"
            )

    embed = Embed(title=title, colour=Colors.error)
    if not out:
        embed.description = t.no_permissions
        await reply(ctx, embed=embed)
        return

    embed.colour = Colors.Permissions
    for (_, name), lines in sorted(out.items(), reverse=True):
        embed.add_field(name=name, value="\n".join(sorted(lines)), inline=False)

    await send_long_embed(ctx, embed, paginate=True)


class PermissionLevelConverter(Converter):
    async def convert(self, ctx: Context, argument: str) -> BasePermissionLevel:
        for level in Config.PERMISSION_LEVELS:  # type: BasePermissionLevel
            if argument.lower() in level.aliases or argument == str(level.level):
                return level

        raise BadArgument(t.invalid_permission_level)


class PermissionsCog(Cog, name="Permissions"):
    CONTRIBUTORS = [Contributor.Defelo, Contributor.wolflu]

    async def on_ready(self):
        pipe = redis.pipeline()
        async for key in redis.scan_iter("permissions:*"):
            pipe.delete(key)
        await pipe.execute()

    permissions = SlashCommandGroup(
        name="permissions",
        description="Bot-Berechtigungen anzeigen und verwalten",
        guild_only=True,
    )

    @permissions.command(name="list", description="Alle Befehls-Berechtigungen auflisten")
    @PermissionsPermission.view_all.check
    async def permissions_list(
        self,
        ctx: ApplicationContext,
        min_level: str | None = Option(str, description="Minimale Berechtigungsstufe (optional)", required=False, default=None),
    ):
        parsed_level = None
        if min_level:
            try:
                parsed_level = await PermissionLevelConverter().convert(ctx, min_level)
            except BadArgument:
                raise CommandError(t.invalid_permission_level)
        if parsed_level is None:
            parsed_level = Config.DEFAULT_PERMISSION_LEVEL

        await list_permissions(ctx, t.permissions_title, parsed_level)

    @permissions.command(name="my", description="Eigene Berechtigungen auflisten")
    @PermissionsPermission.view_own.check
    async def permissions_my(self, ctx: ApplicationContext):
        min_level: BasePermissionLevel = await Config.PERMISSION_LEVELS.get_permission_level(ctx.author)
        await list_permissions(ctx, t.my_permissions_title, min_level)

    @permissions.command(name="set", description="Berechtigungsstufe für eine Permission konfigurieren")
    @PermissionsPermission.manage.check
    async def permissions_set(
        self,
        ctx: ApplicationContext,
        permission_name: str = Option(str, description="Name der Permission"),
        level: str = Option(str, description="Neue Berechtigungsstufe"),
    ):
        try:
            parsed_level = await PermissionLevelConverter().convert(ctx, level)
        except BadArgument:
            raise CommandError(t.invalid_permission_level)

        for permission in get_permissions():
            if permission.fullname.lower() == permission_name.lower():
                break
        else:
            raise CommandError(t.invalid_permission)

        max_level: BasePermissionLevel = await Config.PERMISSION_LEVELS.get_permission_level(ctx.author)
        if max(parsed_level.level, (await permission.resolve()).level) > max_level.level:
            raise CommandError(t.cannot_manage_permission_level)

        await permission.set(parsed_level)

        description = permission.fullname, parsed_level.description
        embed = Embed(title=t.permissions_title, colour=Colors.Permissions, description=t.permission_set(*description))
        await reply(ctx, embed=embed)
        await send_to_changelog(ctx.guild, t.log_permission_set(*description))

    @permissions.command(name="levels", description="Verfügbare Berechtigungsstufen anzeigen")
    @PermissionsPermission.view_all.check
    async def permissions_permission_levels(self, ctx: ApplicationContext):
        embed = Embed(title=t.permission_levels, color=Colors.Permissions)

        for level in Config.PERMISSION_LEVELS:  # type: BasePermissionLevel
            description = t.aliases(", ".join(f"`{alias}`" for alias in level.aliases))
            description += "\n" + t.level(level.level)

            granted_by: list[str] = [f"`{gp}`" for gp in level.guild_permissions]
            for role_name in level.roles:
                role: Optional[Role] = ctx.guild.get_role(await RoleSettings.get(role_name))
                if role:
                    granted_by.append(role.mention)

            if not level.level:
                granted_by = ["@everyone"]

            if granted_by:
                description += "\n" + t.granted_by + "\n" + "\n".join(f":small_orange_diamond: {x}" for x in granted_by)

            embed.add_field(name=level.description, value=description, inline=False)

        await send_long_embed(ctx, embed, paginate=True)
