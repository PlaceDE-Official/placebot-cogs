import asyncio
from datetime import timedelta
from typing import Optional

from discord import Embed, Forbidden, Message, NotFound, Option, SlashCommandGroup, TextChannel
from discord.commands import ApplicationContext
from discord.ext import tasks
from discord.ext.commands import CommandError
from discord.utils import utcnow

from PyDrocsid.cog import Cog
from PyDrocsid.command import reply
from PyDrocsid.database import db, db_wrapper, select
from PyDrocsid.embeds import send_long_embed
from PyDrocsid.translations import t
from PyDrocsid.types import GuildMessageable

from .colors import Colors
from .models import AutoClearChannel
from .permissions import AutoClearPermission
from ...contributor import Contributor
from ...pubsub import send_alert, send_to_changelog


tg = t.g
t = t.autoclear


async def clear_channel(channel: GuildMessageable, minutes: int, limit: Optional[int] = None):
    if not channel.permissions_for(channel.guild.me).read_message_history:
        await send_alert(channel.guild, t.cannot_read(channel.mention))
        return

    message: Message
    async for message in channel.history(before=utcnow() - timedelta(minutes=minutes), limit=limit, oldest_first=True):
        if message.pinned:
            continue

        try:
            await message.delete()
        except Forbidden:
            await send_alert(message.guild, t.not_deleted(channel.mention))
            break
        except NotFound:
            pass


class AutoClearCog(Cog, name="AutoClear"):
    CONTRIBUTORS = [Contributor.Florian, Contributor.Defelo]

    async def on_ready(self):
        try:
            self.loop.start()
        except Exception as e:
            print(e)
            self.loop.restart()

    @tasks.loop(minutes=5)
    @db_wrapper
    async def loop(self):
        autoclear: AutoClearChannel
        async for autoclear in await db.stream(select(AutoClearChannel)):
            channel = self.bot.get_channel(autoclear.channel)
            if not channel:
                await db.delete(autoclear)
                continue

            asyncio.create_task(clear_channel(channel, autoclear.minutes, limit=200))

    autoclear = SlashCommandGroup(
        name="autoclear",
        description="Automatisches Leeren von Kanälen konfigurieren",
        guild_only=True,
    )

    @autoclear.command(name="overview", description="Übersicht aller Autoclear-Kanäle")
    @AutoClearPermission.read.check
    async def autoclear_overview(self, ctx: ApplicationContext):
        embed = Embed(title=t.autoclear, colour=Colors.AutoClear)
        out = []
        async for autoclear in await db.stream(select(AutoClearChannel)):
            channel: Optional[GuildMessageable] = self.bot.get_channel(autoclear.channel)
            if not channel:
                await db.delete(autoclear)
                continue

            out.append(f":small_orange_diamond: {channel.mention} ({t.minutes(cnt=autoclear.minutes)})")

        if not out:
            embed.description = t.no_autoclear_channels
            embed.colour = Colors.error
        else:
            embed.description = "\n".join(out)

        await send_long_embed(ctx, embed=embed)

    @autoclear.command(name="set", description="Autoclear für einen Kanal aktivieren/aktualisieren")
    @AutoClearPermission.write.check
    async def set(
        self,
        ctx: ApplicationContext,
        channel: TextChannel = Option(TextChannel, description="Kanal"),
        minutes: int = Option(int, description="Intervall in Minuten"),
    ):
        if not 0 < minutes < (1 << 31):
            raise CommandError(tg.invalid_duration)

        if not channel.permissions_for(channel.guild.me).manage_messages:
            raise CommandError(t.cannot_add)

        row = await db.get(AutoClearChannel, channel=channel.id)
        if not row:
            await AutoClearChannel.create(channel.id, minutes)
        else:
            row.minutes = minutes

        await send_to_changelog(ctx.guild, t.set(channel.mention, cnt=minutes))
        await reply(ctx, content=f":white_check_mark: Autoclear für {channel.mention} auf {minutes} Minuten gesetzt.")

    @autoclear.command(name="disable", description="Autoclear für einen Kanal deaktivieren")
    @AutoClearPermission.write.check
    async def disable(self, ctx: ApplicationContext, channel: TextChannel = Option(TextChannel, description="Kanal")):
        row = await db.get(AutoClearChannel, channel=channel.id)
        if not row:
            raise CommandError(t.not_configured)

        await db.delete(row)
        await send_to_changelog(ctx.guild, t.disabled(channel.mention))
        await reply(ctx, content=f":white_check_mark: Autoclear für {channel.mention} deaktiviert.")
