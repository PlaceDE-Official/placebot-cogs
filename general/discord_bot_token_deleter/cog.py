import re
import base64

from discord import Message, Embed, Forbidden, NotFound, HTTPException

from PyDrocsid.cog import Cog
from PyDrocsid.material_colors import MaterialColors
from PyDrocsid.translations import t
from ...contributor import Contributor
from ...pubsub import send_alert

tg = t.g
t = t.discord_bot_token_deleter


class DiscordBotTokenDeleterCog(Cog, name="Discord Bot Token Deleter"):
    CONTRIBUTORS = [Contributor.Tert0]
    RE_DC_TOKEN = re.compile(r"([A-Za-z\d\-_]+)\.[A-Za-z\d\-_]+\.[A-Za-z\d\-_]+")

    async def on_message(self, message: Message):
        """Delete a message if it contains a Discord bot token"""

        if message.author.id == self.bot.user.id or not message.guild:
            return
        if not (discord_bot_tokens := self.RE_DC_TOKEN.findall(message.content)):
            return
        has_discord_bot_tokens = False
        for discord_bot_token in discord_bot_tokens:
            if base64.b64decode(re.match(r"[A-Za-z\d]+", discord_bot_token).group(0)).isdigit():
                has_discord_bot_tokens = True
                break
        if not has_discord_bot_tokens:
            return
        embed = Embed(title=t.title, colour=MaterialColors.bluegrey)
        embed.description = t.description
        await message.channel.send(message.author.mention, embed=embed)
        try:
            await message.delete()
        except Forbidden:
            await send_alert(message.guild, t.not_deleted(message.jump_url, message.channel.mention))
