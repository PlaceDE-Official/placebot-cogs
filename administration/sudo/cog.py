import sys

from discord import CustomActivity, Embed, Member, Message, SlashCommandGroup, Status
from discord.commands import ApplicationContext
from discord.ext.commands import Context, check
from discord.utils import utcnow

from PyDrocsid.bot_mode import BotMode, get_mode_change_message, mode_args, write_status
from PyDrocsid.cog import Cog
from PyDrocsid.command import reply
from PyDrocsid.config import Config
from PyDrocsid.environment import SUDOERS
from PyDrocsid.events import call_event_handlers
from PyDrocsid.logger import get_logger
from PyDrocsid.material_colors import MaterialColors
from PyDrocsid.redis_client import redis
from PyDrocsid.translations import t
from PyDrocsid.types import GuildMessageable
from PyDrocsid.util import get_owners, is_sudoer

from .permissions import SudoPermission
from ...contributor import Contributor


tg = t.g
t = t.sudo

logger = get_logger(__name__)


@check
async def can_see_sudo(ctx: Context | ApplicationContext) -> bool:
    if is_sudoer(ctx):
        return True
    return False


class SudoCog(Cog, name="Sudo"):
    CONTRIBUTORS = [Contributor.Defelo, Contributor.TNT2k]

    def __init__(self):
        super().__init__()
        self.sudo_cache: dict[(GuildMessageable, Member), Message] = {}

    async def on_command_error(self, ctx: Context, _):
        if ctx.author.id in SUDOERS:
            self.sudo_cache[(ctx.channel, ctx.author)] = ctx.message

    sudo = SlashCommandGroup(
        name="sudo",
        description="Wartungs- und Administrationsbefehle",
        guild_only=True,
    )

    @sudo.command(name="show_sudoers", description="Alle Sudoer anzeigen")
    @SudoPermission.show_sudoers.check
    async def show_sudoers(self, ctx: ApplicationContext):
        """
        Shows all users, which can run the `sudo` command itself (without subcommands from this cog).
        """
        sudoers = "\n".join({ctx.guild.owner.mention} | set(map(lambda x: f"<@{x}>", SUDOERS)))
        embed = Embed(color=MaterialColors.blue, title="Sudoers", description=t.sudoers_list(sudoers))
        await reply(ctx, embed=embed)

    @sudo.command(name="clear_cache", description="Redis-Cache leeren")
    @SudoPermission.clear_cache.check
    async def clear_cache(self, ctx: ApplicationContext):
        """
        Clears the redis cache.
        """
        await redis.flushdb()
        await reply(ctx, content=":white_check_mark: Redis-Cache erfolgreich geleert.")

    @sudo.command(name="reload", description="Alle Cogs des Bots neu laden")
    @SudoPermission.reload.check
    async def reload(self, ctx: ApplicationContext):
        """
        Reloads all cogs of the bot.
        Configs are not re-read.
        Will not stop any running actions.
        """
        await call_event_handlers("ready")
        Config.LAST_RELOAD = utcnow()
        await reply(ctx, content=":white_check_mark: Cogs neu geladen.")

    @sudo.command(name="restart", description="Bot komplett neu starten")
    @SudoPermission.restart.check
    async def restart(self, ctx: ApplicationContext):
        """
        Restarts the bot completely.
        Will stop all running actions.
        """
        message = t.bot_restart(*mode_args(ctx))
        for owner in get_owners(self.bot):
            embed = Embed(color=MaterialColors.error, title=tg.bot_mode_change, description=message)
            await owner.send(embed=embed)
        logger.warning(message)
        await reply(ctx, content=":white_check_mark: Bot wird neu gestartet...")
        await self.bot.close()
        exit(1)

    @sudo.command(name="stop", description="Bot ordnungsgemäß stoppen")
    @SudoPermission.stop.check
    async def stop(self, ctx: ApplicationContext):
        """
        Stops the bot gracefully.
        """
        Config.BOT_MODE = BotMode.STOPPED
        message = get_mode_change_message(ctx)
        for owner in get_owners(self.bot):
            embed = Embed(color=MaterialColors.error, title=tg.bot_mode_change, description=message)
            await owner.send(embed=embed)
        logger.warning(message)
        await write_status(message, BotMode.STOPPED)
        await reply(ctx, content=":white_check_mark: Bot wird gestoppt.")
        await self.bot.close()

    @sudo.command(name="kill", description="Bot sofort beenden")
    @SudoPermission.kill.check
    async def kill(self, ctx: ApplicationContext):
        """
        Kills the bot immediately.
        """
        Config.BOT_MODE = BotMode.KILLED
        message = get_mode_change_message(ctx)
        logger.warning(message)
        await write_status(message, BotMode.KILLED)
        await reply(ctx, content=":skull: Bot-Prozess wird beendet.")
        sys.exit(1)

    @sudo.command(name="maintenance", description="Wartungsmodus umschalten")
    @SudoPermission.toggle_maintenance.check
    async def maintenance(self, ctx: ApplicationContext):
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
        await write_status(message, Config.BOT_MODE)

        if Config.BOT_MODE.bot_activity:
            await ctx.bot.change_presence(
                status=Status.online, activity=CustomActivity(name=Config.BOT_MODE.bot_activity)
            )
        else:
            await ctx.bot.change_presence(status=Status.online, activity=None)
        await reply(ctx, embed=embed)
