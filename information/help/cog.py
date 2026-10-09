from typing import Any, List, Optional, Union

from discord import Embed, Message, Option, slash_command
from discord.commands import ApplicationCommand, ApplicationContext, SlashCommand, SlashCommandGroup
from discord.ext import commands
from discord.ext.commands import Command, CommandError, Context, Group

from PyDrocsid.cog import Cog, get_documentation
from PyDrocsid.command import can_run_command, docs, get_optional_permissions
from PyDrocsid.config import Contributor
from PyDrocsid.embeds import send_long_embed
from PyDrocsid.permission import BasePermission, BasePermissionLevel
from PyDrocsid.translations import t

from .colors import Colors


tg = t.g
t = t.help


async def send_help(ctx: Context | ApplicationContext, command_name: Optional[Union[str, Command, ApplicationCommand]] = None) -> List[Message]:
    def format_command(cmd: Any) -> str:
        doc = getattr(cmd, "short_doc", None) or getattr(cmd, "description", None)
        doc_str = f" - {doc}" if doc else ""
        if isinstance(cmd, (SlashCommand, SlashCommandGroup)):
            return f"`/{cmd.name}`{doc_str}"
        return f"`{cmd.name}`{doc_str}"

    async def add_commands(cog_name: str, cmds: List[Any]):
        desc: List[str] = []
        seen = set()
        for cmd in sorted(cmds, key=lambda c: c.name):
            if cmd.name in seen:
                continue
            if not getattr(cmd, "hidden", False) and await can_run_command(cmd, ctx):
                callback = getattr(cmd, "_callback", getattr(cmd, "callback", None))
                emoji = getattr(callback, "emoji", ":small_orange_diamond:")
                desc.append(emoji + " " + format_command(cmd))
                seen.add(cmd.name)
        if desc:
            embed.add_field(name=cog_name, value="\n".join(desc), inline=False)

    prefix = getattr(ctx, "prefix", "/") or "/"

    embed = Embed(title=t.help, color=Colors.help)
    if command_name is None:
        for cog in sorted(ctx.bot.cogs.values(), key=lambda c: c.qualified_name):
            await add_commands(cog.qualified_name, cog.get_commands())
        await add_commands(t.no_category, [command for command in ctx.bot.commands if command.cog is None])

        embed.add_field(name="** **", value=t.help_usage(prefix), inline=False)

        return await send_long_embed(ctx, embed, paginate=True, max_fields=8)

    if isinstance(command_name, str):
        if command_name.startswith("/"):
            command_name = command_name[1:]

        cog: Optional[Cog] = ctx.bot.get_cog(command_name)
        if cog is not None:
            await add_commands(cog.qualified_name, cog.get_commands())
            #if doc_url := get_documentation(cog):
            #    embed.add_field(name=t.documentation, value=doc_url, inline=False)
            return await send_long_embed(ctx, embed)

        command: Any = ctx.bot.get_command(command_name)
        if command is None and hasattr(ctx.bot, "get_application_command"):
            command = ctx.bot.get_application_command(command_name)
        if command is None and hasattr(ctx.bot, "pending_application_commands"):
            for cmd in ctx.bot.pending_application_commands:
                if cmd.name == command_name:
                    command = cmd
                    break
        if command is None:
            raise CommandError(t.cog_or_command_not_found)
    else:
        command = command_name

    if not await can_run_command(command, ctx):
        raise CommandError(tg.not_allowed)

    description = prefix
    if getattr(command, "full_parent_name", None):
        description += command.full_parent_name + " "
    aliases = getattr(command, "aliases", None)
    if aliases:
        description += "[" + "|".join([command.name] + aliases) + "] "
    else:
        description += command.name + " "

    signature = getattr(command, "signature", None)
    if signature is None and hasattr(command, "options"):
        signature = " ".join(f"<{opt.name}>" if opt.required else f"[{opt.name}]" for opt in command.options)
    description += signature or ""

    embed.description = f"```css\n{description.strip()}\n```"
    doc_text = getattr(command, "help", None) or getattr(command, "description", None)
    if doc_text:
        embed.add_field(name=t.description, value=doc_text, inline=False)

    if isinstance(command, Group):
        await add_commands(t.subcommands, command.commands)
    elif hasattr(command, "subcommands") and command.subcommands:
        await add_commands(t.subcommands, command.subcommands)

    permissions: list[str] = []
    permission_levels: list[BasePermissionLevel] = []

    cmds = []
    cmd = command
    while cmd:
        cmds.append(cmd)
        cmd = getattr(cmd, "parent", None)

    for cmd in reversed(cmds):
        for check in getattr(cmd, "checks", []):
            permission: Union[BasePermission, BasePermissionLevel, None] = getattr(check, "level", None)
            if isinstance(permission, BasePermission):
                permissions.append(permission.fullname)
            elif isinstance(permission, BasePermissionLevel):
                permission_levels.append(permission)

    if permissions:
        embed.add_field(
            name=t.required_permissions,
            value="\n".join(f":small_orange_diamond: `{p}`" for p in permissions),
            inline=False,
        )
    if permission_levels:
        permission_level: BasePermissionLevel = max(permission_levels, key=lambda pl: pl.level)
        if permission_level.level > 0:
            embed.add_field(
                name=t.required_permission_level,
                value=f":small_orange_diamond: **{permission_level.description}**",
                inline=False,
            )

    callback = getattr(command, "callback", None)
    optional_perms = getattr(callback, "optional_permissions", []) if callback else []
    optional_permissions: list[str] = [permission.fullname for permission in optional_perms]
    if optional_permissions:
        embed.add_field(
            name=t.optional_permissions,
            value="\n".join(f":small_blue_diamond: `{p}`" for p in optional_permissions),
            inline=False,
        )

    #if (doc_url := get_documentation(cmd.cog)) and not getattr(cmd.callback, "no_documentation", False):
    #    embed.add_field(name=t.documentation, value=doc_url, inline=False)

    return await send_long_embed(ctx, embed)


class HelpCog(Cog, name="Help"):
    CONTRIBUTORS = [Contributor.Defelo, Contributor.ce_phox]

    @slash_command(name="help", description="Hilfe zu Befehlen und Modulen anzeigen")
    async def help_slash(
        self,
        ctx: ApplicationContext,
        cog_or_command: str | None = Option(str, description="Name eines Befehls oder Cogs (optional)", required=False, default=None),
    ):
        await send_help(ctx, cog_or_command)

    @commands.command()
    @docs(t.commands.help)
    async def help(self, ctx: Context, *, cog_or_command: Optional[str] = None):
        await send_help(ctx, cog_or_command)

