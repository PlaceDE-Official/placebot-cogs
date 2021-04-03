from datetime import datetime
from typing import Optional, List

from discord import Role, Member, Guild, Embed
from discord.ext import commands
from discord.ext.commands import Context, CommandError, CheckFailure, check, guild_only, UserInputError

from PyDrocsid.cog import Cog
from PyDrocsid.database import db_thread, db
from PyDrocsid.settings import Settings
from PyDrocsid.translations import t
from .colors import Colors
from .models import VerificationRole
from .permissions import VerificationPermission
from cogs.library.contributor import Contributor
from cogs.library.pubsub import send_to_changelog

tg = t.g
t = t.verification


@check
async def private_only(ctx: Context):
    if ctx.guild is not None:
        raise CheckFailure(t.private_only)

    return True


class VerificationCog(Cog, name="Verification"):
    CONTRIBUTORS = [Contributor.Defelo, Contributor.wolflu]
    PERMISSIONS = VerificationPermission

    @commands.command()
    @private_only
    async def verify(self, ctx: Context, *, password: str):
        correct_password: str = await Settings.get(str, "verification_password")
        if correct_password is None:
            raise CommandError(t.verification_disabled)

        if password != correct_password:
            raise CommandError(t.password_incorrect)

        guild: Guild = self.bot.guilds[0]
        member: Member = guild.get_member(ctx.author.id)

        delay: int = await Settings.get(int, "verification_delay", -1)
        if delay != -1 and (datetime.utcnow() - member.joined_at).total_seconds() < delay:
            raise CommandError(t.password_incorrect)

        add: List[Role] = []
        remove: List[Role] = []
        fail = False
        for vrole in await db_thread(db.all, VerificationRole):  # type: VerificationRole
            role: Optional[Role] = guild.get_role(vrole.role_id)
            if role is None:
                continue

            if vrole.reverse:
                if role in member.roles:
                    remove.append(role)
                else:
                    fail = True
            elif not vrole.reverse and role not in member.roles:
                add.append(role)
        if not add and not remove:
            raise CommandError(t.already_verified)
        if fail:
            raise CommandError(t.verification_reverse_role_not_assigned)

        await member.add_roles(*add)
        await member.remove_roles(*remove)
        embed = Embed(title=t.verification, description=t.verified, colour=Colors.Verification)
        await ctx.send(embed=embed)

    @commands.group(aliases=["vf"])
    @VerificationPermission.manage.check
    @guild_only()
    async def verification(self, ctx: Context):
        """
        configure verify command
        """

        if ctx.subcommand_passed is not None:
            if ctx.invoked_subcommand is None:
                raise UserInputError
            return

        password: str = await Settings.get(str, "verification_password")

        normal: List[Role] = []
        reverse: List[Role] = []
        for vrole in await db_thread(db.all, VerificationRole):  # type: VerificationRole
            role: Optional[Role] = ctx.guild.get_role(vrole.role_id)
            if role is None:
                await db_thread(db.delete, vrole)
            else:
                [normal, reverse][vrole.reverse].append(role)

        embed = Embed(title=t.verification, colour=Colors.error)
        if password is None or not normal + reverse:
            embed.add_field(name=tg.status, value=t.verification_disabled, inline=False)
            await ctx.send(embed=embed)
            return

        embed.colour = Colors.Verification
        embed.add_field(name=tg.status, value=t.verification_enabled, inline=False)
        embed.add_field(name=t.password, value=f"`{password}`", inline=False)

        delay: int = await Settings.get(int, "verification_delay", -1)
        val = t.x_seconds(cnt=delay) if delay != -1 else tg.disabled
        embed.add_field(name=tg.delay, value=val, inline=False)

        if normal:
            embed.add_field(
                name=t.roles_normal,
                value="\n".join(f":small_orange_diamond: {role.mention}" for role in normal),
            )
        if reverse:
            embed.add_field(
                name=t.roles_reverse,
                value="\n".join(f":small_blue_diamond: {role.mention}" for role in reverse),
            )

        await ctx.send(embed=embed)

    @verification.command(name="add", aliases=["a", "+"])
    async def verification_add(self, ctx: Context, role: Role, reverse: bool = False):
        """
        add verification role
        if `reverse` is set to `true`, the role is not added but removed during verification.
        the `verify` command will fail if the user does not have the role.
        """

        if role >= ctx.me.top_role:
            raise CommandError(t.role_not_set_too_high(role, ctx.me.top_role))
        if role.managed:
            raise CommandError(t.role_not_set_managed_role(role))

        if await db_thread(db.get, VerificationRole, role.id) is not None:
            raise CommandError(t.verification_role_already_set)

        await db_thread(VerificationRole.create, role.id, reverse)
        embed = Embed(
            title=t.verification,
            description=t.verification_role_added,
            colour=Colors.Verification,
        )
        await ctx.send(embed=embed)
        if reverse:
            await send_to_changelog(ctx.guild, t.log_verification_role_added_reverse(role.name, role.id))
        else:
            await send_to_changelog(ctx.guild, t.log_verification_role_added(role.name, role.id))

    @verification.command(name="remove", aliases=["r", "-"])
    async def verification_remove(self, ctx: Context, *, role: Role):
        """
        remove verification role
        """

        if (row := await db_thread(db.get, VerificationRole, role.id)) is None:
            raise CommandError(t.verification_role_not_set)

        await db_thread(db.delete, row)
        embed = Embed(
            title=t.verification,
            description=t.verification_role_removed,
            colour=Colors.Verification,
        )
        await ctx.send(embed=embed)
        await send_to_changelog(ctx.guild, t.log_verification_role_removed(role.name, role.id))

    @verification.command(name="password", aliases=["p"])
    async def verification_password(self, ctx: Context, *, password: str):
        """
        configure verification password
        """

        if len(password) > 256:
            raise CommandError(t.password_too_long)

        await Settings.set(str, "verification_password", password)
        embed = Embed(
            title=t.verification,
            description=t.verification_password_configured,
            colour=Colors.Verification,
        )
        await ctx.send(embed=embed)
        await send_to_changelog(ctx.guild, t.log_verification_password_configured(password))

    @verification.command(name="delay", aliases=["d"])
    async def verification_delay(self, ctx: Context, seconds: int):
        """
        configure verification delay
        set to -1 to disable
        """

        if seconds != -1 and not 0 <= seconds < (1 << 31):
            raise CommandError(tg.invalid_duration)

        await Settings.set(int, "verification_delay", seconds)
        embed = Embed(title=t.verification, colour=Colors.Verification)
        if seconds == -1:
            embed.description = t.verification_delay_disabled
            await send_to_changelog(ctx.guild, t.verification_delay_disabled)
        else:
            embed.description = t.verification_delay_configured
            await send_to_changelog(ctx.guild, t.log_verification_delay_configured(cnt=seconds))
        await ctx.send(embed=embed)
