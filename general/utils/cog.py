import itertools
import random
import string
from typing import Optional, Union

from discord import Embed, Emoji, Member, User
from discord.ext import commands
from discord.ext.commands import CommandError, Context, guild_only, max_concurrency
from discord.utils import format_dt, snowflake_time

from PyDrocsid.async_thread import run_in_thread
from PyDrocsid.cog import Cog
from PyDrocsid.command import docs, reply
from PyDrocsid.config import Config
from PyDrocsid.converter import Color, EmojiConverter, UserMemberConverter
from PyDrocsid.material_colors import MaterialColors
from PyDrocsid.translations import t
from PyDrocsid.util import measure_latency

from .colors import Colors
from .permissions import UtilsPermission
from ...contributor import Contributor


tg = t.g
t = t.utils


@run_in_thread
def generate_color(colors: list[tuple[float, float, float]], n: int, a: float) -> tuple[float, float, float]:
    guess = [random.random() for _ in range(3)]  # noqa: S311
    last = None
    for _ in range(n):
        new_guess = guess.copy()
        for i in range(3):
            mx = 0
            for c in colors:
                b = 10 ** (-3 - sum(x in (0, 1) for x in c))
                error = 1 / (b + sum((p - q) ** 2 for p, q in zip(guess, c)))
                if error > mx:
                    mx = error
                    new_guess[i] = guess[i] + (
                        2 * (guess[i] - c[i]) * ((b + sum((p - q) ** 2 for p, q in zip(guess, c))) ** -2) * a
                    )
        guess = [min(max(x, 0), 1) for x in new_guess]

        if last == guess:
            return guess[0], guess[1], guess[2]
        last = guess.copy()

    return guess[0], guess[1], guess[2]


class UtilsCog(Cog, name="Utils"):
    CONTRIBUTORS = [Contributor.Defelo]

    @commands.command()
    @docs(t.commands.ping)
    async def ping(self, ctx: Context):
        latency: Optional[float] = measure_latency()
        embed = Embed(title=t.pong, colour=Colors.Utils)
        if latency is not None:
            embed.description = t.pong_latency(latency * 1000)
        await reply(ctx, embed=embed)

    @commands.command(aliases=["sf", "time"])
    @docs(t.commands.snowflake)
    async def snowflake(self, ctx: Context, arg: int):
        if arg < 0:
            raise CommandError(t.invalid_snowflake)

        try:
            await reply(ctx, format_dt(snowflake_time(arg), style="F"))
        except (OverflowError, ValueError, OSError):
            raise CommandError(t.invalid_snowflake)

    @commands.command(aliases=["enc"])
    @docs(t.commands.encode)
    async def encode(self, ctx: Context, *, user: UserMemberConverter):
        user: Union[User, Member]

        embed = Embed(color=Colors.Utils)
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.add_field(name=t.username, value=str(user.name.encode())[2:-1], inline=False)
        if isinstance(user, Member) and user.nick:
            embed.add_field(name=t.nickname, value=str(user.nick.encode())[2:-1], inline=False)

        await reply(ctx, embed=embed)

    @commands.command(aliases=["rc"])
    @UtilsPermission.suggest_role_color.check
    @max_concurrency(1)
    @guild_only()
    @docs(t.commands.suggest_role_color)
    async def suggest_role_color(self, ctx: Context, *avoid: Color):
        avoid: tuple[int]

        colors = [hex(color)[2:].zfill(6) for role in ctx.guild.roles if (color := role.color.value)]
        colors += [hex(c)[2:].zfill(6) for c in avoid]
        colors = [[int(x, 16) / 255 for x in [c[:2], c[2:4], c[4:]]] for c in colors]
        colors += itertools.product(range(2), repeat=3)

        color = await generate_color(colors, 2000, 5e-5)
        color = "%02X" * 3 % tuple([round(float(c) * 255) for c in color])

        embed = Embed(title="#" + color, color=int(color, 16))
        embed.set_image(url=f"https://singlecolorimage.com/get/{color}/400x100")
        await reply(ctx, embed=embed)

    @commands.command()
    async def emoji_id(self, ctx: Context, emoji: EmojiConverter):
        emoji: Emoji
        await reply(ctx, content=str(emoji.id))

    @commands.command()
    async def uptime(self, ctx: Context):
        embed = Embed(colour=Colors.Utils, title=t.uptime.title)
        embed.add_field(name=t.uptime.started, value=Config.STARTED.strftime("%d.%m.%Y %H:%M:%S"), inline=False)
        embed.add_field(name=t.uptime.last_reload, value=Config.LAST_RELOAD.strftime("%d.%m.%Y %H:%M:%S"), inline=False)
        embed.set_footer(text=t.uptime.utc)
        await reply(ctx, embed=embed)

    @commands.command(aliases=["kesk", "cookie"])
    async def keks(self, ctx: Context):
        if ctx.channel.id not in [1153276001619546193]:
            return
        embed = Embed(title="Nächste Kekslieferungszeit:", colour=MaterialColors.blue)
        embed.set_image(
            url=f"https://kekse.tnt2k.de/kekszeitraum.png?{''.join([random.choice(string.ascii_letters) for _ in range(10)])}"
        )
        embed.description = "*Es kann **bis zu 10 Minuten** dauern, bis das Bild zu den echten Daten passt.*"
        embed.set_footer(text="Danke an master, butterkatze, golem, funkie, moinkas, shrimp und elias!")
        await reply(ctx, embed=embed)
