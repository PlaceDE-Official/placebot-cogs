import sys
import threading

from discord import Message, TextChannel, Member, Embed, CheckFailure, Status, Game
from discord.ext import commands
from discord.ext.commands import Context, check, Command, CommandError

from PyDrocsid.bot_mode import write_status, get_mode_change_message, BotMode, mode_args
from PyDrocsid.cog import Cog
from PyDrocsid.command import can_run_command
from PyDrocsid.config import Config
from PyDrocsid.emojis import name_to_emoji
from PyDrocsid.environment import SUDOERS
from PyDrocsid.events import call_event_handlers
from PyDrocsid.logger import get_logger
from PyDrocsid.material_colors import MaterialColors
from PyDrocsid.permission import permission_override
from PyDrocsid.redis_client import redis
from PyDrocsid.translations import t
from PyDrocsid.util import get_owners, is_sudoer
from .permissions import SudoPermission
from ...contributor import Contributor

tg = t.g
t = t.sudo

logger = get_logger(__name__)


@check
async def can_see_sudo(ctx: Context) -> bool:
    if is_sudoer(ctx):
        return True
    cmd: Command
    for cmd in ctx.cog.__cog_commands__:
        if not cmd.hidden and await can_run_command(cmd, ctx):
            return True
    raise CommandError(t.not_in_sudoers_file(ctx.author.mention))


class SudoCog(Cog, name="Sudo"):
    CONTRIBUTORS = [Contributor.Defelo, Contributor.TNT2k]

    def __init__(self):
        super().__init__()
        self.sudo_cache: dict[(TextChannel, Member), Message] = {}

    async def on_command_error(self, ctx: Context, _):
        if ctx.author.id in SUDOERS:
            self.sudo_cache[(ctx.channel, ctx.author)] = ctx.message

    @commands.group(hidden=True)
    @can_see_sudo
    async def sudo(self, ctx: Context):
        """
        A few commands which can do maintenance stuff.

        Subcommands with extra permissions do not need sudo permissions to be used.

        Run `sudo <CMD>` with any bot command to override ALL user related permissions checks once for that command.
        You need extra sudo permissions to do this.
        This permission can not be given or revoked using the bot itself.

        All successful sudo commands are logged to console.
        """
        if is_sudoer(ctx):
            permission_override.set(Config.SUDO)
        if ctx.invoked_subcommand is None:
            if not is_sudoer(ctx):
                raise CheckFailure(t.not_in_sudoers_file(ctx.author.mention))
            message: Message = ctx.message
            cmd = ctx.message.content.lstrip(ctx.prefix + ctx.command.name).strip()
            message.content = ctx.prefix + cmd

            if cmd == "!!" and (msg := self.sudo_cache.pop((ctx.channel, ctx.author), None)):
                message.content = msg.content

            logger.info(
                f"User {ctx.author.mention} ({ctx.author.name} / {ctx.author.id}) invoked sudo command: {message.content}")

            await self.bot.process_commands(message)
        else:
            logger.info(
                f"User {ctx.author.mention} ({ctx.author.name} / {ctx.author.id}) invoked sudo command: {ctx.message.content}")

    @sudo.command(aliases=["sudoers"])
    @SudoPermission.show_sudoers.check
    async def show_sudoers(self, ctx: Context):
        """
        Shows all users, which can run the `sudo` command itself (without subcommands from this cog).
        """
        sudoers = "\n".join({ctx.guild.owner.mention} | set(map(lambda x: f"<@{x}>", SUDOERS)))
        embed = Embed(color=MaterialColors.blue, title="Sudoers", description=t.sudoers_list(sudoers))
        await ctx.reply(embed=embed)

    @sudo.command()
    @SudoPermission.clear_cache.check
    async def clear_cache(self, ctx: Context):
        """
        Clears the redis cache.
        """
        await redis.flushdb()
        await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @sudo.command()
    @SudoPermission.reload.check
    async def reload(self, ctx: Context):
        """
        Reloads all cogs of the bot.
        Configs are not re-read.
        Will not stop any running actions.
        """
        await call_event_handlers("ready")
        await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @sudo.command()
    @SudoPermission.restart.check
    async def restart(self, ctx: Context):
        """
        Restarts the bot completely.
        Will stop all running actions.
        """
        message = t.bot_restart(*mode_args(ctx))
        for owner in get_owners(self.bot):
            embed = Embed(color=MaterialColors.error, title=tg.bot_mode_change, description=message)
            await owner.send(embed=embed)
        logger.warning(message)
        await ctx.message.add_reaction(name_to_emoji["white_check_mark"])
        await self.bot.close()
        exit(1)

    @sudo.command()
    @SudoPermission.stop.check
    async def stop(self, ctx: Context):
        """
        Stops the bot gracefully.
        """
        Config.BOT_MODE = BotMode.STOPPED
        message = get_mode_change_message(ctx)
        for owner in get_owners(self.bot):
            embed = Embed(color=MaterialColors.error, title=tg.bot_mode_change, description=message)
            await owner.send(embed=embed)
        logger.warning(message)
        write_status(message)
        await ctx.message.add_reaction(name_to_emoji["white_check_mark"])
        await self.bot.close()

    @sudo.command()
    @SudoPermission.kill.check
    async def kill(self, ctx: Context):
        """
        Kills the bot immediately.
        """

        def kill_thread_func():
            Config.BOT_MODE = BotMode.KILLED
            message = get_mode_change_message(ctx)
            logger.warning(message)
            write_status(message)

        th = threading.Thread(target=kill_thread_func)
        th.start()
        sys.exit(1)

    @sudo.command()
    @SudoPermission.toggle_maintenance.check
    async def maintenance(self, ctx: Context):
        """
        Toggles maintenance mode of the bot.
        Users without sudo or `maintenance_bypass` permission can not interact with the bot in any meaningful way.
        """
        Config.BOT_MODE = BotMode.MAINTENANCE if Config.BOT_MODE == BotMode.NORMAL else BotMode.NORMAL
        message = get_mode_change_message(ctx)
        embed = Embed(color=MaterialColors.error, title=tg.bot_mode_change, description=message)
        for owner in get_owners(self.bot):
            await owner.send(embed=embed)
        logger.warning(message)
        write_status(message)

        if Config.BOT_MODE.bot_activity:
            await ctx.bot.change_presence(status=Status.online, activity=Game(name=Config.BOT_MODE.bot_activity))
        else:
            await ctx.bot.change_presence(status=Status.online, activity=None)
        await ctx.reply(embed=embed)
        await ctx.message.add_reaction(name_to_emoji["white_check_mark"])
