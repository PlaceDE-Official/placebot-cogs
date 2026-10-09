import string

from discord import Embed, Option, slash_command
from discord.commands import ApplicationContext
from discord.ext.commands import CommandError, guild_only

from PyDrocsid.cog import Cog
from PyDrocsid.command import reply
from PyDrocsid.prefix import set_prefix
from PyDrocsid.translations import t

from .colors import Colors
from .permissions import SettingsPermission
from ...contributor import Contributor
from ...pubsub import send_to_changelog


tg = t.g
t = t.settings


class SettingsCog(Cog, name="Settings"):
    CONTRIBUTORS = [Contributor.Defelo]

    @slash_command(name="prefix", description="Präfix für Text-Befehle ändern")
    @SettingsPermission.change_prefix.check
    @guild_only()
    async def change_prefix(
        self,
        ctx: ApplicationContext,
        new_prefix: str = Option(str, description="Neues Befehlspräfix (1-16 Zeichen)"),
    ):
        if not 0 < len(new_prefix) <= 16:
            raise CommandError(t.invalid_prefix_length)

        valid_chars = set(string.ascii_letters + string.digits + string.punctuation) - {"`"}
        if any(c not in valid_chars for c in new_prefix):
            raise CommandError(t.prefix_invalid_chars)

        await set_prefix(new_prefix)
        embed = Embed(title=t.prefix, description=t.prefix_updated, colour=Colors.prefix)
        await reply(ctx, embed=embed)
        await send_to_changelog(ctx.guild, t.log_prefix_updated(new_prefix))
