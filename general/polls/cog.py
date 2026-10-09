import re
import string
from typing import List, Optional, Tuple

from discord import Embed, Forbidden, Interaction, Member, Message, Option, PartialEmoji, Role, Status, slash_command
from discord.commands import ApplicationContext
from discord.ext.commands import CommandError, Context, guild_only
from discord.utils import utcnow

from PyDrocsid.cog import Cog
from PyDrocsid.embeds import EmbedLimits, send_long_embed
from PyDrocsid.emojis import emoji_to_name, name_to_emoji
from PyDrocsid.events import StopEventHandling
from PyDrocsid.settings import RoleSettings
from PyDrocsid.translations import t
from PyDrocsid.util import check_wastebasket

from .colors import Colors
from .permissions import PollsPermission
from ...contributor import Contributor


tg = t.g
t = t.polls

MAX_OPTIONS = 20  # Discord reactions limit

default_emojis = [name_to_emoji[f"regional_indicator_{x}"] for x in string.ascii_lowercase]


def status_icon(status: Status) -> str:
    return {
        Status.online: ":green_circle:",
        Status.idle: ":yellow_circle:",
        Status.dnd: ":red_circle:",
        Status.offline: ":black_circle:",
    }[status]


async def get_rolepoll_embed(message: Message) -> Tuple[Optional[Embed], Optional[int], Optional[str]]:
    out = [None, None, ""]
    for embed in message.embeds:
        for i, field in enumerate(embed.fields):
            if t.status == field.name:
                out[0] = embed
                out[1] = i
            if t.roles == field.name:
                out[2] = field.value
            if all(out):
                return tuple(out)
    return tuple(out)


async def send_poll(
    ctx: Context, title: str, args: str, fields: List[Tuple[str, str]] | None = None, allow_delete: bool = True
):
    question, *options = [line.replace("\x00", "\n") for line in args.replace("\\\n", "\x00").split("\n") if line]

    if not options:
        raise CommandError(t.missing_options)
    if fields is None:
        fields = []
    if len(options) > MAX_OPTIONS - allow_delete:
        raise CommandError(t.too_many_options(MAX_OPTIONS - allow_delete))

    options = [PollOption(ctx, line, i) for i, line in enumerate(options)]

    if any(len(str(option)) > EmbedLimits.FIELD_VALUE for option in options):
        raise CommandError(t.option_too_long(EmbedLimits.FIELD_VALUE))

    embed = Embed(title=title, description=question, color=Colors.Polls, timestamp=utcnow())
    embed.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
    if allow_delete:
        embed.set_footer(text=t.created_by(ctx.author, ctx.author.id), icon_url=ctx.author.display_avatar.url)

    if len({x.emoji for x in options}) < len(options):
        raise CommandError(t.option_duplicated)

    for option in options:
        embed.add_field(name="** **", value=str(option), inline=False)

    for field in fields:
        embed.add_field(name=field[0], value=field[1], inline=False)

    poll: Message
    if isinstance(ctx, ApplicationContext):
        res = await ctx.respond(embed=embed)
        poll = await res.original_response() if isinstance(res, Interaction) else res
    else:
        poll = await ctx.send(embed=embed)

    try:
        for option in options:
            await poll.add_reaction(option.emoji)
        if allow_delete:
            await poll.add_reaction(name_to_emoji["wastebasket"])
    except Forbidden:
        raise CommandError(t.could_not_add_reactions(ctx.channel.mention))


class PollsCog(Cog, name="Polls"):
    CONTRIBUTORS = [Contributor.MaxiHuHe04, Contributor.Defelo, Contributor.TNT2k, Contributor.wolflu]

    def __init__(self, team_roles: list[str]):
        self.team_roles: list[str] = team_roles

    async def get_reacted_users(self, roles: list[Role], message: Optional[Message] = None) -> str:
        users: list[Member] = await self.get_missing_poll_members(roles, message)
        if not users:
            return t.rolepoll_all_voted

        users.sort(key=lambda m: str(m).lower())

        *users, last = (x.mention for x in users)
        users: list[str]
        value = t.users_missing(teamlers=", ".join(users), last=last, cnt=len(users) + 1)
        if len(value) >= EmbedLimits.FIELD_VALUE:
            value = t.too_many_users
        return value

    async def get_missing_poll_members(self, roles: list[Role], message: Optional[Message] = None) -> List[Member]:
        users: set[Member] = set()
        for role in roles:
            users.update(member for member in role.members if not member.bot)

        if message:
            for reaction in message.reactions:
                if reaction.me:
                    users.difference_update(await reaction.users().flatten())

        return list(users)

    async def get_roles_for_poll(self, message: Message = None, roles_string: str = "") -> list[Role]:
        if message:
            pollstatus, index, roles_string = await get_rolepoll_embed(message)
            if pollstatus is None:
                return []

        roles = []
        for role_string in roles_string.splitlines():
            if role := self.bot.guilds[0].get_role(int(role_string.replace("<@&", "").replace(">", ""))):
                roles.append(role)
        return roles

    async def can_use_poll(self, member: Member, message: Message) -> bool:
        return any(member in role.members for role in await self.get_roles_for_poll(message))

    async def on_raw_reaction_add(self, message: Message, emoji: PartialEmoji, member: Member):
        if member.bot or message.guild is None:
            return

        if await check_wastebasket(message, member, emoji, t.created_by, PollsPermission.delete):
            await message.delete()
            raise StopEventHandling

        pollstatus, index, roles_string = await get_rolepoll_embed(message)
        if pollstatus is None:
            return

        if not await self.can_use_poll(member, message):
            try:
                await message.remove_reaction(emoji, member)
            except Forbidden:
                pass
            raise StopEventHandling

        for reaction in message.reactions:
            if reaction.emoji == emoji.name:
                break
        else:
            return

        if not reaction.me:
            return

        value = await self.get_reacted_users(await self.get_roles_for_poll(roles_string=roles_string), message)
        pollstatus.set_field_at(index, name=t.status, value=value, inline=False)
        await message.edit(embed=pollstatus)

    async def on_raw_reaction_remove(self, message: Message, _, member: Member):
        if member.bot or message.guild is None:
            return

        pollstatus, index, roles_string = await get_rolepoll_embed(message)
        if pollstatus is None:
            return
        if pollstatus is not None:
            user_reacted = False
            for reaction in message.reactions:
                if reaction.me and member in await reaction.users().flatten():
                    user_reacted = True
                    break
            if not user_reacted and await self.can_use_poll(member, message):
                value = await self.get_reacted_users(await self.get_roles_for_poll(roles_string=roles_string), message)
                pollstatus.set_field_at(index, name=t.status, value=value, inline=False)
                await message.edit(embed=pollstatus)
                return

    @slash_command(name="poll", description="Erstelle eine Umfrage")
    @guild_only()
    async def poll(
        self,
        ctx: ApplicationContext,
        question: str = Option(str, description="Titel / Frage der Umfrage"),
        options: str = Option(str, description="Antwortoptionen (durch Zeilenumbruch oder Semikolon getrennt)"),
    ):
        """
        Starts a poll. Multiline options can be specified using a `\\` at the end of a line
        """
        raw_options = [opt.strip() for opt in re.split(r"[\n;]", options) if opt.strip()]
        args = question + "\n" + "\n".join(raw_options)
        await send_poll(ctx, t.poll, args)

    @slash_command(name="teampoll", description="Erstelle eine Team-Umfrage")
    @PollsPermission.team_poll.check
    @guild_only()
    async def teampoll(
        self,
        ctx: ApplicationContext,
        question: str = Option(str, description="Titel / Frage der Team-Umfrage"),
        options: str = Option(str, description="Antwortoptionen (durch Zeilenumbruch oder Semikolon getrennt)"),
    ):
        """
        Starts a poll and shows, which teamler has not voted yet.
         Multiline options can be specified using a `\\` at the end of a line
        """
        raw_options = [opt.strip() for opt in re.split(r"[\n;]", options) if opt.strip()]
        args = question + "\n" + "\n".join(raw_options)
        roles = []
        for role_name in self.team_roles:
            if team_role := self.bot.guilds[0].get_role(await RoleSettings.get(role_name)):
                roles.append(team_role)
        await send_poll(
            ctx,
            t.team_poll,
            args,
            fields=[
                (t.roles, "\n".join(role.mention for role in roles)),
                (t.status, await self.get_reacted_users(roles)),
            ],
            allow_delete=False,
        )

    @slash_command(name="rolepoll", description="Erstelle eine rollenbasierte Umfrage")
    @PollsPermission.role_poll.check
    @guild_only()
    async def rolepoll(
        self,
        ctx: ApplicationContext,
        role: Role = Option(Role, description="Berechtigte Rolle"),
        question: str = Option(str, description="Titel / Frage der Umfrage"),
        options: str = Option(str, description="Antwortoptionen (durch Zeilenumbruch oder Semikolon getrennt)"),
    ):
        """
        Starts a poll and shows, which users with at least one given role have not voted yet.
         Multiline options can be specified using a `\\` at the end of a line
        """
        raw_options = [opt.strip() for opt in re.split(r"[\n;]", options) if opt.strip()]
        args = question + "\n" + "\n".join(raw_options)
        roles = [role]
        await send_poll(
            ctx,
            t.rolepoll,
            args,
            fields=[
                (t.roles, "\n".join(r.mention for r in roles)),
                (t.status, await self.get_reacted_users(roles)),
            ],
            allow_delete=False,
        )

    @slash_command(name="yesno", description="Erstelle eine Ja/Nein-Umfrage")
    @guild_only()
    async def yesno(self, ctx: ApplicationContext, question: str = Option(str, description="Frage für die Abstimmung")):
        """
        adds thumbsup and thumbsdown reactions to the message
        """
        embed = Embed(title="Ja / Nein", description=question, color=Colors.Polls, timestamp=utcnow())
        embed.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
        res = await ctx.respond(embed=embed)
        msg: Message = await res.original_response() if isinstance(res, Interaction) else res

        try:
            await msg.add_reaction(name_to_emoji["thumbsup"])
            await msg.add_reaction(name_to_emoji["thumbsdown"])
        except Forbidden:
            raise CommandError(t.could_not_add_reactions(ctx.channel.mention))

    @slash_command(name="team_yesno", description="Erstelle eine Team-Ja/Nein-Umfrage")
    @guild_only()
    async def team_yesno(self, ctx: ApplicationContext, question: str = Option(str, description="Frage für die Abstimmung")):
        """
        Starts a yes/no poll and shows, which teamler has not voted yet.
        """
        roles = []
        for role_name in self.team_roles:
            if team_role := self.bot.guilds[0].get_role(await RoleSettings.get(role_name)):
                roles.append(team_role)
        embed = Embed(title=t.team_poll, description=question, color=Colors.Polls, timestamp=utcnow())
        embed.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)

        embed.add_field(name=t.roles, value="\n".join(role.mention for role in roles), inline=False)
        embed.add_field(name=t.status, value=await self.get_reacted_users(roles), inline=False)

        res = await ctx.respond(embed=embed)
        message: Message = await res.original_response() if isinstance(res, Interaction) else res
        try:
            await message.add_reaction(name_to_emoji["+1"])
            await message.add_reaction(name_to_emoji["-1"])
        except Forbidden:
            raise CommandError(t.could_not_add_reactions(ctx.channel.mention))

    @slash_command(name="pollmembers", description="Zeigt Mitglieder an, die noch nicht abgestimmt haben", guild_only=True)
    async def pollmembers(self, ctx: ApplicationContext, message_id: str = Option(str, description="Nachrichten-ID der Umfrage")):
        """
        Shows which users did not react to a restricted poll.
        """
        try:
            msg_id = int(message_id.strip())
            message = await ctx.channel.fetch_message(msg_id)
        except (ValueError, Exception):
            raise CommandError("Nachricht im aktuellen Kanal nicht gefunden!")

        if not self.can_use_poll(ctx.author, message):
            raise CommandError(t.role_poll_not_allowed)

        members: list[Member] = await self.get_missing_poll_members(await self.get_roles_for_poll(message), message)

        members.sort(
            key=lambda m: ([Status.online, Status.idle, Status.dnd, Status.offline].index(m.status), str(m), m.id)
        )

        out = []
        for member in members:
            out.append(f"{status_icon(member.status)} {member.mention} (@{member})")

        if out:
            embed = Embed(title=t.member_list_cnt(cnt=len(out)), colour=0x256BE6, description="\n".join(out))
        else:
            embed = Embed(title=t.member_list, colour=0xCF0606, description=t.no_members)
        await send_long_embed(ctx, embed, paginate=True)


class PollOption:
    def __init__(self, ctx: Context, line: str, number: int):
        if not line:
            raise CommandError(t.empty_option)

        emoji_candidate, *text = line.lstrip().split(" ")
        text = " ".join(text)

        custom_emoji_match = re.fullmatch(r"<a?:[a-zA-Z0-9_]+:(\d+)>", emoji_candidate)
        if custom_emoji := ctx.bot.get_emoji(int(custom_emoji_match.group(1))) if custom_emoji_match else None:
            self.emoji = custom_emoji
            self.option = text.strip()
        elif (unicode_emoji := emoji_candidate) in emoji_to_name:
            self.emoji = unicode_emoji
            self.option = text.strip()
        elif (match := re.match(r"^:([^: ]+):$", emoji_candidate)) and (
            unicode_emoji := name_to_emoji.get(match.group(1).replace(":", ""))
        ):
            self.emoji = unicode_emoji
            self.option = text.strip()
        else:
            self.emoji = default_emojis[number]
            self.option = line

        if name_to_emoji["wastebasket"] == self.emoji:
            raise CommandError(t.can_not_use_wastebucket_as_option)

    def __str__(self):
        return f"{self.emoji} {self.option}" if self.option else str(self.emoji)
