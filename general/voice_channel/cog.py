from __future__ import annotations

import asyncio
import random
from os import getenv
from pathlib import Path

from discord import (
    CategoryChannel,
    Embed,
    Forbidden,
    Guild,
    HTTPException,
    Interaction,
    InteractionResponse,
    Member,
    Message,
    NotFound,
    PermissionOverwrite,
    Role,
    TextChannel,
    VoiceChannel,
    VoiceState,
    ui,
)
from discord.abc import Messageable
from discord.ext import commands, tasks
from discord.ext.commands import CommandError, Context, Greedy, UserInputError, guild_only
from discord.ui import Button
from discord.utils import format_dt, utcnow

from PyDrocsid.async_thread import GatherAnyError, gather_any, run_as_task
from PyDrocsid.cog import Cog
from PyDrocsid.command import Confirmation, MaintenanceAwareView, docs, optional_permissions, reply
from PyDrocsid.database import db, db_context, db_wrapper, delete, filter_by, select
from PyDrocsid.embeds import send_long_embed
from PyDrocsid.emojis import name_to_emoji
from PyDrocsid.logger import get_logger
from PyDrocsid.multilock import MultiLock, ReentrantMultiLock
from PyDrocsid.prefix import get_prefix
from PyDrocsid.redis_client import redis
from PyDrocsid.settings import RoleSettings
from PyDrocsid.translations import t
from PyDrocsid.util import DynamicVoiceConverter, check_role_assignable, escape_codeblock, send_editable_log

from .colors import Colors
from .models import AllowedChannelName, DynChannel, DynChannelMember, DynGroup, RoleVoiceLink
from .permissions import VoiceChannelPermission
from .settings import DynamicVoiceSettings
from ...contributor import Contributor
from ...pubsub import send_alert, send_to_changelog


tg = t.g
t = t.voice_channel

Overwrites = dict[Member | Role, PermissionOverwrite]

join_requests = MultiLock[int]()
channel_locks = ReentrantMultiLock[int]()

logger = get_logger(__name__)


def merge_permission_overwrites(overwrites: Overwrites, *args: tuple[Member | Role, PermissionOverwrite]) -> Overwrites:
    out = {k: PermissionOverwrite.from_pair(*v.pair()) for k, v in overwrites.items()}
    for k, v in args:
        out.setdefault(k, PermissionOverwrite()).update(**{p: q for p, q in v if q is not None})
    return out


def check_voice_permissions(voice_channel: VoiceChannel, role: Role) -> bool:
    view_channel = voice_channel.overwrites_for(role).view_channel
    connect = voice_channel.overwrites_for(role).connect
    if view_channel is None:
        view_channel = role.permissions.view_channel
    if connect is None:
        connect = role.permissions.connect
    return view_channel and connect


async def collect_links(guild: Guild, link_set, channel_id):
    link: RoleVoiceLink
    async for link in await db.stream(filter_by(RoleVoiceLink, voice_channel=channel_id)):
        if role := guild.get_role(link.role):
            link_set.add(role)


async def update_roles(member: Member, *, add: set[Role] = None, remove: set[Role] = None):
    add = add or set()
    remove = remove or set()
    add, remove = add - remove, remove - add

    for role in remove:
        try:
            await member.remove_roles(role)
        except Forbidden:
            await send_alert(member.guild, t.could_not_remove_roles(role.mention, member.mention))

    for role in add:
        try:
            await member.add_roles(role)
        except Forbidden:
            await send_alert(member.guild, t.could_not_add_roles(role.mention, member.mention))


async def get_commands_embed() -> Embed:
    return Embed(
        title=t.dyn_voice_help_title,
        color=Colors.Voice,
        description=t.dyn_voice_help_content(prefix=await get_prefix()),
    )


async def get_warning_embed() -> Embed:
    return Embed(title=t.warning, color=Colors.warning, description=t.channel_readability_warning)


async def rename_channel(channel: TextChannel | VoiceChannel, name: str):
    try:
        idx, _ = await gather_any(channel.edit(name=name), asyncio.sleep(3))
    except GatherAnyError as e:
        raise e.exception

    if idx:
        raise CommandError(t.rename_rate_limit)


def get_user_role(guild: Guild, channel: DynChannel) -> Role | None:
    return guild.get_role(channel.group.user_role)


def remove_lock_overrides(
    channel: DynChannel,
    voice_channel: VoiceChannel,
    overwrites: Overwrites,
    *,
    keep_members: bool,
    reset_user_role: bool,
) -> Overwrites:
    me = voice_channel.guild.me
    overwrites = {
        k: v
        for k, v in overwrites.items()
        if not isinstance(k, Member) or k == me or (keep_members and k in voice_channel.members)
    }
    if not reset_user_role:
        return overwrites

    user_role = voice_channel.guild.get_role(channel.group.user_role)
    overwrites = merge_permission_overwrites(overwrites, (user_role, PermissionOverwrite(view_channel=True)))
    overwrites[user_role].update(connect=None)
    return overwrites


async def safe_create_voice_channel(
    category: CategoryChannel | Guild, channel: DynChannel, name: str, overwrites: Overwrites
) -> VoiceChannel:
    guild: Guild = category.guild if isinstance(category, CategoryChannel) else category
    user_role: Role = get_user_role(guild, channel)

    voice_channel = None
    try:
        voice_channel = await category.create_voice_channel(name, overwrites=overwrites)
    except Forbidden:
        pass

    if not voice_channel:
        ov = overwrites.pop(user_role, None)
        voice_channel: VoiceChannel = await category.create_voice_channel(name, overwrites=overwrites)

        if ov:
            overwrites[user_role] = ov
            await voice_channel.edit(overwrites=overwrites)

    await voice_channel.send(embed=await get_commands_embed())
    await voice_channel.send(embed=await get_warning_embed())

    return voice_channel


class ControlMessage(MaintenanceAwareView):
    def __init__(self, cog: VoiceChannelCog, channel: DynChannel, message: Message):
        super().__init__(timeout=None)
        self.cog = cog
        self.channel = channel
        self.message = message

        _, locked, hidden, no_ping = self.get_status()

        self.children: list[Button]
        self.children[2].label = t.buttons["unlock" if locked else "lock"]
        self.children[2].emoji = name_to_emoji["unlock" if locked else "lock"]
        self.children[3].label = t.buttons["show" if hidden else "hide"]
        self.children[3].emoji = name_to_emoji["eye" if hidden else "man_detective"]
        self.children[4].label = t.buttons["ping" if no_ping else "no_ping"]
        self.children[4].emoji = name_to_emoji["bell" if no_ping else "no_bell"]

    async def update(self):
        self.channel = await DynChannel.get(channel_id=self.channel.channel_id)

    def get_status(self):
        voice_channel: VoiceChannel = self.cog.bot.get_channel(self.channel.channel_id)
        user_role = voice_channel.guild.get_role(self.channel.group.user_role)
        locked = self.channel.locked
        no_ping = self.channel.no_ping
        hidden = voice_channel.overwrites_for(user_role).view_channel is False
        return voice_channel, locked, hidden, no_ping

    @ui.button(label=t.buttons.info, emoji=name_to_emoji["information_source"])
    @db_wrapper
    async def info(self, _, interaction: Interaction):
        async with channel_locks[self.channel.channel_id]:
            await self.cog.send_voice_info(interaction.response, self.channel)

    @ui.button(label=t.buttons.help, emoji=name_to_emoji["grey_question"])
    async def help(self, _, interaction: Interaction):
        await interaction.response.send_message(embed=await get_commands_embed(), ephemeral=True)

    @ui.button()
    @db_wrapper
    async def lock(self, _, interaction: Interaction):
        await self.update()
        try:
            await self.cog.check_authorization(self.channel, interaction.user)
        except CommandError:
            await interaction.response.send_message(t.private_voice_owner_required, ephemeral=True)
            return

        async with channel_locks[self.channel.channel_id]:
            voice_channel, locked, _, _ = self.get_status()
            if not locked:
                await self.cog.lock_channel(interaction.user, self.channel, voice_channel, hide=False)
            else:
                await self.cog.unlock_channel(interaction.user, self.channel, voice_channel)
            await interaction.response.defer()

    @ui.button()
    @db_wrapper
    async def hide(self, _, interaction: Interaction):
        await self.update()
        try:
            await self.cog.check_authorization(self.channel, interaction.user)
        except CommandError:
            await interaction.response.send_message(t.private_voice_owner_required, ephemeral=True)
            return

        async with channel_locks[self.channel.channel_id]:
            voice_channel, _, hidden, _ = self.get_status()
            if not hidden:
                await self.cog.lock_channel(interaction.user, self.channel, voice_channel, hide=True)
            else:
                await self.cog.unhide_channel(interaction.user, self.channel, voice_channel)
            await interaction.response.defer()

    @ui.button()
    @db_wrapper
    async def ping(self, _, interaction: Interaction):
        await self.update()
        try:
            await self.cog.check_authorization(self.channel, interaction.user)
        except CommandError:
            await interaction.response.send_message(t.private_voice_owner_required, ephemeral=True)
            return

        async with channel_locks[self.channel.channel_id]:
            voice_channel, _, hidden, no_ping = self.get_status()
            await self.cog.change_channel_ping(interaction.user, self.channel, no_ping=not no_ping)
            await interaction.response.defer()


def _recurse_name_check(
    s: str,
    allowed: dict[str, set[str]],
    dp: list[bool],
    name_parts: list[tuple[str, str]],
    result: list[list[tuple[str, str]]],
    require_whitespaces: bool = True,
):
    for i in reversed(range(len(s))):
        if s[i] == " " and (i + 1 >= len(s) or dp[i + 1]):
            dp[i] = True
            continue
        if dp[i]:
            continue
        for filename, names in allowed.items():
            for w in names:
                # wenn etwas passt
                if (
                    s[i : i + len(w)] == w
                    and (i + len(w) >= len(s) or dp[i + len(w)] and (not require_whitespaces or s[i + len(w)] == " "))
                    and not any(dp[i : i + len(w)])
                ):
                    dp_copy = dp.copy()
                    dp_copy[i : i + len(w)] = [True] * len(w)
                    name_parts_copy = name_parts.copy()
                    name_parts_copy.append((filename, w))
                    # wenn das wort vollständig ist -> keine weitere recursion, ergebnis speichern
                    if dp_copy[0]:
                        result.append(list(reversed(name_parts_copy)))
                    else:
                        # wenn das wort unvollständig ist -> recursion mit dem gefundenen ende
                        _recurse_name_check(s, allowed, dp_copy, name_parts_copy, result, require_whitespaces)


class VoiceChannelCog(Cog, name="Voice Channels"):
    CONTRIBUTORS = [
        Contributor.Defelo,
        Contributor.Florian,
        Contributor.wolflu,
        Contributor.TNT2k,
        # vc name lists only:
        Contributor.Scriptim,
        Contributor.MarcelCoding,
        Contributor.Felux,
        Contributor.hackandcode,
    ]

    def __init__(self, team_roles: list[str]):
        self.team_roles: list[str] = team_roles
        self._owners: dict[int, Member] = {}

        self._join_tasks: dict[tuple[Member, VoiceChannel], asyncio.Task] = {}
        self._leave_tasks: dict[tuple[Member, VoiceChannel], asyncio.Task] = {}
        self._recent_kicks: set[tuple[Member, VoiceChannel]] = set()
        self.custom_names = set()

        names = getenv("VOICE_CHANNEL_NAMES", "*")
        if names == "*":
            name_lists = [file.name.removesuffix(".txt") for file in Path(__file__).parent.joinpath("names").iterdir()]
        else:
            name_lists = names.split(",")

        self.names: dict[str, set[str]] = {}
        for name_list in name_lists:
            self.names[name_list] = set()
            with Path(__file__).parent.joinpath(f"names/{name_list}.txt").open() as file:
                for name in file.readlines():
                    if name := name.strip():
                        self.names[name_list].add(name)

        self.allowed_names: dict[str, set[str]] = {}
        for path in Path(__file__).parent.joinpath("names").iterdir():
            if not path.name.endswith(".txt"):
                continue

            with path.open() as file:
                self.allowed_names.update({path.stem: set(map(lambda x: x.strip().lower(), file.readlines()))})

    def prepare(self) -> bool:
        return bool(self.names)

    def check_name(self, name, find_all, require_whitespaces) -> tuple[bool, list[list[tuple[str, str]]]]:
        s = name.lower()
        allowed = self.allowed_names.copy()
        allowed.update({"custom": self.custom_names})
        dp = [False for _ in s]
        if not find_all:
            for i in reversed(range(len(s))):
                if s[i] == " " and (i + 1 >= len(s) or dp[i + 1]):
                    dp[i] = True
                    continue
                for _, names in allowed.items():
                    for w in names:
                        if s[i : i + len(w)] == w and (
                            i + len(w) >= len(s) or dp[i + len(w)] and (not require_whitespaces or s[i + len(w)] == " ")
                        ):
                            dp[i] = True
                            break
            return dp[0], []
        all_name_fragments: list[list[tuple[str, str]]] = []
        _recurse_name_check(s.lower(), allowed, dp, [], all_name_fragments, require_whitespaces)
        return bool(all_name_fragments), all_name_fragments

    def _get_name_list(self, guild_id: int) -> str:
        r = random.Random(f"{guild_id}{utcnow().date().isoformat()}")
        return r.choice(sorted(self.names))

    def _random_channel_name(self, guild_id: int, avoid: set[str]) -> str | None:
        names = self.names[self._get_name_list(guild_id)]
        allowed = list({*names} - avoid)
        if allowed and random.randrange(100):
            return random.choice(allowed)

        a = "acddfilmmrtneeelnoioanopflofckrztrhetri  pu2aolain  hpkkxo "
        a += "ai  ea     nt  ul      y  st          u          f          f           "
        c = len(b := [*range(13 - 37 + 42 + ((4 > 2) << 4 - 2) >> (1 & 3 & 3 & 7 & ~42))])
        return random.shuffle(b) or next((e for d in b if (e := a[d::c].strip()) not in avoid), None)

    async def get_channel_name(self, guild: Guild) -> str:
        return self._random_channel_name(guild.id, {channel.name for channel in guild.voice_channels})

    async def is_teamler(self, member: Member) -> bool:
        return any(
            team_role in member.roles
            for role_name in self.team_roles
            if (team_role := member.guild.get_role(await RoleSettings.get(role_name))) is not None
        )

    def get_text_channel(self, channel: DynChannel) -> TextChannel | None:
        return self.bot.get_channel(channel.text_id)

    def get_voice_channel(self, channel: DynChannel) -> VoiceChannel:
        return self.bot.get_channel(channel.channel_id)

    async def get_owner_from_cache(self, channel: DynChannel) -> Member | None:
        if out := self._owners.get(channel.channel_id):
            return out

        self._owners[channel.channel_id] = await self.fetch_owner_from_db(channel)
        return self._owners[channel.channel_id]

    async def fetch_owner_from_db(self, channel: DynChannel) -> Member | None:
        voice_channel: VoiceChannel = self.bot.get_channel(channel.channel_id)

        if channel.owner_override and any(channel.owner_override == member.id for member in voice_channel.members):
            return voice_channel.guild.get_member(channel.owner_override)

        owner: DynChannelMember | None = await db.get(DynChannelMember, id=channel.owner_id)
        if owner and any(owner.member_id == member.id for member in voice_channel.members):
            return voice_channel.guild.get_member(owner.member_id)

        return await self.fix_owner(channel)

    async def fix_owner(self, dyn_channel: DynChannel) -> Member | None:
        voice_channel: VoiceChannel = self.bot.get_channel(dyn_channel.channel_id)

        in_voice = {m.id for m in voice_channel.members}
        for m in dyn_channel.members:
            if m.member_id in in_voice:
                member = voice_channel.guild.get_member(m.member_id)
                if member.bot:
                    continue

                dyn_channel.owner_id = m.id
                return await self.cache_owner(dyn_channel, member)

        dyn_channel.owner_id = None
        return await self.cache_owner(dyn_channel, None)

    async def cache_owner(self, channel: DynChannel, new_owner: Member | None) -> Member | None:
        old_owner: Member | None = self._owners.get(channel.channel_id)

        if not new_owner:
            self._owners.pop(channel.channel_id, None)
        elif old_owner != new_owner:
            self._owners[channel.channel_id] = new_owner
            await self.send_voice_msg(channel, t.voice_channel, [t.voice_owner_changed(new_owner.mention)])

        return new_owner

    async def send_voice_msg(self, channel: DynChannel, title: str, msgs: list[str], force_new_embed: bool = False):
        try:
            voice_channel: VoiceChannel = self.get_voice_channel(channel)
        except CommandError as e:
            await send_alert(self.bot.guilds[0], *e.args)
            return

        color = int([Colors.unlocked, Colors.locked][channel.locked])
        now = format_dt(now := utcnow(), style="D") + " " + format_dt(now, style="T")
        try:
            message: Message = await send_editable_log(
                voice_channel,
                title,
                "",
                [(now, line) for line in msgs],
                colour=color,
                force_new_embed=force_new_embed,
                force_new_field=True,
            )
        except Forbidden:
            await send_alert(voice_channel.guild, t.could_not_send_voice_msg(voice_channel.mention))
            return

        await self.update_control_message(channel, message)

    async def update_control_message(self, channel: DynChannel, message: Message):
        async def clear_view(msg_id):
            try:
                await (await message.channel.fetch_message(msg_id)).edit(view=None)
            except Forbidden:
                await send_alert(message.guild, t.could_not_clear_reactions(message.jump_url, message.channel.mention))
            except NotFound:
                pass

        if (msg := await redis.get(key := f"dynvc_control_message:{channel.text_id}")) and msg != str(message.id):
            asyncio.create_task(clear_view(msg))

        await redis.setex(key, 86400, message.id)

        await message.edit(view=ControlMessage(self, channel, message))

    async def check_authorization(self, channel: DynChannel, member: Member):
        if await VoiceChannelPermission.override_owner.check_permissions(member):
            return

        if await self.get_owner_from_cache(channel) == member:
            return

        raise CommandError(t.private_voice_owner_required)

    async def get_channel(
        self,
        member: Member,
        *,
        check_owner: bool,
        check_locked: bool = False,
        channel: VoiceChannel | TextChannel | None = None,
    ) -> tuple[DynChannel, VoiceChannel, TextChannel | None]:
        if not channel and member.voice is not None and member.voice.channel is not None:
            channel = member.voice.channel
        if not channel:
            raise CommandError(t.not_in_voice)

        if isinstance(channel, TextChannel):
            db_channel: DynChannel | None = await db.get(
                DynChannel, [DynChannel.group, DynGroup.channels], DynChannel.members, text_id=channel.id
            )
        else:
            db_channel: DynChannel | None = await db.get(
                DynChannel, [DynChannel.group, DynGroup.channels], DynChannel.members, channel_id=channel.id
            )

        if not db_channel:
            raise CommandError(t.not_a_dynamic_channel)

        text_channel = self.get_text_channel(db_channel)
        voice_channel = self.get_voice_channel(db_channel)

        if check_locked and not db_channel.locked:
            raise CommandError(t.channel_not_locked)

        if check_owner:
            await self.check_authorization(db_channel, member)

        return db_channel, voice_channel, text_channel

    async def on_ready(self):
        guild: Guild = self.bot.guilds[0]

        role_voice_links: dict[Role, list[VoiceChannel]] = {}

        link: RoleVoiceLink
        async for link in await db.stream(select(RoleVoiceLink)):
            role: Role | None = guild.get_role(link.role)
            if role is None:
                await db.delete(link)
                continue

            if link.voice_channel.isnumeric():
                voice: VoiceChannel | None = guild.get_channel(int(link.voice_channel))
                if not voice:
                    await db.delete(link)
                else:
                    role_voice_links.setdefault(role, []).append(voice)
            else:
                group: DynGroup | None = await db.get(DynGroup, DynGroup.channels, id=link.voice_channel)
                if not group:
                    await db.delete(link)
                    continue

                for channel in group.channels:
                    if voice := guild.get_channel(channel.channel_id):
                        role_voice_links.setdefault(role, []).append(voice)

        role_changes: dict[Member, tuple[set[Role], set[Role]]] = {}
        for role, channels in role_voice_links.items():
            members = set()
            for channel in channels:
                members.update(channel.members)
            for member in members:
                if role not in member.roles:
                    role_changes.setdefault(member, (set(), set()))[0].add(role)
            for member in role.members:
                if member not in members:
                    role_changes.setdefault(member, (set(), set()))[1].add(role)

        for member, (add, remove) in role_changes.items():
            asyncio.create_task(update_roles(member, add=add, remove=remove))

        async for item in await db.stream(select(AllowedChannelName)):
            self.custom_names.add(item.name)

        try:
            self.vc_loop.start()
        except Exception as e:
            print(e)
            self.vc_loop.restart()

    @tasks.loop(minutes=30)
    @db_wrapper
    async def vc_loop(self):
        guild: Guild = self.bot.guilds[0]

        channel: DynChannel
        async for channel in await db.stream(select(DynChannel)):
            voice_channel: VoiceChannel | None = guild.get_channel(channel.channel_id)
            if not voice_channel:
                await db.delete(channel)
                continue

            # if not voice_channel.members:
            #     asyncio.create_task(voice_channel.edit(name=await self.get_channel_name(guild)))

    async def lock_channel(self, member: Member, channel: DynChannel, voice_channel: VoiceChannel, *, hide: bool):
        locked = channel.locked
        channel.locked = True
        member_overwrites = [
            (member, PermissionOverwrite(view_channel=True, connect=True)) for member in voice_channel.members
        ]
        overwrites = merge_permission_overwrites(
            voice_channel.overwrites,
            (
                voice_channel.guild.get_role(channel.group.user_role),
                PermissionOverwrite(view_channel=not hide, connect=False),
            ),
            *member_overwrites,
        )

        try:
            await voice_channel.edit(overwrites=overwrites)
        except Forbidden:
            raise CommandError(t.could_not_overwrite_permissions(voice_channel.mention))

        try:
            if text_channel := self.get_text_channel(channel):
                await text_channel.edit(
                    overwrites=merge_permission_overwrites(text_channel.overwrites, *member_overwrites)
                )
        except Forbidden:
            raise CommandError(t.could_not_overwrite_permissions(text_channel.mention))

        if hide:
            await self.send_voice_msg(channel, t.voice_channel, [t.hidden(member.mention)], force_new_embed=not locked)
        else:
            await self.send_voice_msg(channel, t.voice_channel, [t.locked(member.mention)], force_new_embed=True)

    async def unlock_channel(
        self, member: Member | None, channel: DynChannel, voice_channel: VoiceChannel, *, skip_text: bool = False
    ):
        channel.locked = False
        overwrites = remove_lock_overrides(
            channel, voice_channel, voice_channel.overwrites, keep_members=False, reset_user_role=True
        )
        overwrites = merge_permission_overwrites(
            overwrites,
            *[
                (member, PermissionOverwrite(send_messages=True, add_reactions=True))
                for member in voice_channel.members
            ],
        )

        try:
            await voice_channel.edit(overwrites=overwrites)
        except Forbidden:
            raise CommandError(t.could_not_overwrite_permissions(voice_channel.mention))

        if skip_text:
            return

        try:
            if text_channel := self.get_text_channel(channel):
                await text_channel.edit(
                    overwrites=remove_lock_overrides(
                        channel, voice_channel, text_channel.overwrites, keep_members=True, reset_user_role=False
                    )
                )
        except Forbidden:
            raise CommandError(t.could_not_overwrite_permissions(text_channel.mention))

        await self.send_voice_msg(channel, t.voice_channel, [t.unlocked(member.mention)], force_new_embed=True)

    async def change_channel_ping(self, member: Member, channel: DynChannel, *, no_ping: bool):
        channel.no_ping = no_ping
        if no_ping:
            await self.send_voice_msg(channel, t.voice_channel, [t.pings_disabled(member.mention)])
        else:
            await self.send_voice_msg(channel, t.voice_channel, [t.pings_enabled(member.mention)])

    async def unhide_channel(self, member: Member, channel: DynChannel, voice_channel: VoiceChannel):
        user_role = voice_channel.guild.get_role(channel.group.user_role)

        try:
            await voice_channel.set_permissions(user_role, view_channel=True, connect=False, use_activites=True)
        except Forbidden:
            raise CommandError(t.could_not_overwrite_permissions(voice_channel.mention))

        await self.send_voice_msg(channel, t.voice_channel, [t.visible(member.mention)])

    async def add_to_channel(self, channel: DynChannel, voice_channel: VoiceChannel, members: list[Member]):
        overwrites = [
            (member, PermissionOverwrite(view_channel=True, connect=True, send_messages=True, add_reactions=True))
            for member in members
        ]
        try:
            await voice_channel.edit(overwrites=merge_permission_overwrites(voice_channel.overwrites, *overwrites))
        except Forbidden:
            raise CommandError(t.could_not_overwrite_permissions(voice_channel.mention))

        if text_channel := self.get_text_channel(channel):
            try:
                await text_channel.edit(overwrites=merge_permission_overwrites(text_channel.overwrites, *overwrites))
            except Forbidden:
                raise CommandError(t.could_not_overwrite_permissions(text_channel.mention))

        await self.send_voice_msg(channel, t.voice_channel, [t.user_added(member.mention) for member in members])

    async def remove_from_channel(self, channel: DynChannel, voice_channel: VoiceChannel, members: list[Member]):
        overwrites = [
            (member, PermissionOverwrite(view_channel=None, connect=False, send_messages=False, add_reactions=False))
            for member in members
        ]
        try:
            await voice_channel.edit(overwrites=merge_permission_overwrites(voice_channel.overwrites, *overwrites))
        except Forbidden:
            raise CommandError(t.could_not_overwrite_permissions(voice_channel.mention))

        if text_channel := self.get_text_channel(channel):
            try:
                await text_channel.edit(overwrites=merge_permission_overwrites(text_channel.overwrites, *overwrites))
            except Forbidden:
                raise CommandError(t.could_not_overwrite_permissions(text_channel.mention))

        is_owner_flag = False
        for member in members:
            await db.exec(delete(DynChannelMember).filter_by(channel_id=voice_channel.id, member_id=member.id))
            is_owner = member == await self.get_owner_from_cache(channel)
            if member.voice and member.voice.channel == voice_channel:
                try:
                    await member.move_to(None)
                except Forbidden:
                    await send_alert(member.guild, t.could_not_kick(member.mention, voice_channel.mention))
                    is_owner = False
                else:
                    self._recent_kicks.add((member, voice_channel))
            is_owner_flag = is_owner_flag or is_owner

        await self.send_voice_msg(channel, t.voice_channel, [t.user_removed(member.mention) for member in members])
        if is_owner_flag:
            await self.fix_owner(channel)

    async def create_text_channel(
        self, dyn_channel: DynChannel, voice_channel: VoiceChannel, ctx: Context = None
    ) -> TextChannel | None:
        text_channel: TextChannel
        guild: Guild = voice_channel.guild
        category: CategoryChannel | Guild = voice_channel.category or guild
        overwrites = {
            guild.default_role: PermissionOverwrite(read_messages=False, connect=False),
            guild.me: PermissionOverwrite(read_messages=True, manage_channels=True),
        }
        if len(category.channels) >= 50:
            if ctx:
                embed = Embed(
                    colour=Colors.error,
                    description=t.could_not_create_text_channel(voice_channel.mention, t.category_full),
                )
                await ctx.reply(embed=embed)
            else:
                await send_alert(
                    voice_channel.guild, t.could_not_create_text_channel(voice_channel.mention, t.category_full)
                )
            return
        for role_name in self.team_roles:
            if not (team_role := guild.get_role(await RoleSettings.get(role_name))):
                continue
            if check_voice_permissions(voice_channel, team_role):
                overwrites[team_role] = PermissionOverwrite(read_messages=True, send_messages=True, add_reactions=True)
        overwrites.update(
            {member: PermissionOverwrite(view_channel=True, connect=True) for member in voice_channel.members}
        )
        try:
            text_channel = await category.create_text_channel(
                voice_channel.name, topic=t.text_channel_for(voice_channel.mention), overwrites=overwrites
            )
        except (Forbidden, HTTPException) as e:
            logger.warning(e.status_code, e.content)
            if ctx:
                embed = Embed(
                    colour=Colors.error, description=t.could_not_create_text_channel(voice_channel.mention, "")
                )
                await ctx.reply(embed=embed)
            else:
                await send_alert(voice_channel.guild, t.could_not_create_text_channel(voice_channel.mention, ""))
            return
        dyn_channel.text_id = text_channel.id

    async def member_join(self, member: Member, voice_channel: VoiceChannel):
        async with channel_locks[voice_channel.id]:
            dyn_channel: DynChannel | None = await DynChannel.get(channel_id=voice_channel.id)
            if not dyn_channel:
                return

            guild: Guild = voice_channel.guild
            category: CategoryChannel | Guild = voice_channel.category or guild

            # create new
            if all(c.members for chnl in dyn_channel.group.channels if (c := self.bot.get_channel(chnl.channel_id))):
                overwrites = voice_channel.overwrites
                if len(category.channels) >= 50:
                    await send_alert(voice_channel.guild, t.could_not_create_voice_channel(t.category_full))
                else:
                    if dyn_channel.locked:
                        overwrites = remove_lock_overrides(
                            dyn_channel, voice_channel, overwrites, keep_members=False, reset_user_role=True
                        )
                    try:
                        new_channel = await safe_create_voice_channel(
                            category, dyn_channel, await self.get_channel_name(guild), overwrites
                        )
                    except (Forbidden, HTTPException) as e:
                        logger.warning(e.status_code, e.content)
                        await send_alert(voice_channel.guild, t.could_not_create_voice_channel(""))
                    else:
                        await DynChannel.create(new_channel.id, dyn_channel.group_id)

            # create text channel
            text_channel: TextChannel | None = self.bot.get_channel(dyn_channel.text_id)
            if not text_channel and dyn_channel.group.text_channel_by_default:
                text_channel = await self.create_text_channel(dyn_channel, voice_channel)

            if text_channel:
                try:
                    await text_channel.set_permissions(
                        member,
                        overwrite=PermissionOverwrite(read_messages=True, send_messages=True, add_reactions=True),
                    )
                except Forbidden:
                    await send_alert(voice_channel.guild, t.could_not_overwrite_permissions(text_channel.mention))

            if not dyn_channel.locked:
                await voice_channel.set_permissions(
                    member,
                    overwrite=PermissionOverwrite(
                        read_messages=True, connect=True, send_messages=True, add_reactions=True
                    ),
                )
            await self.send_voice_msg(dyn_channel, t.voice_channel, [t.dyn_voice_joined(member.mention)])

            # add member permissions
            if dyn_channel.locked and member not in voice_channel.overwrites:
                try:
                    await self.add_to_channel(dyn_channel, voice_channel, [member])
                except CommandError as e:
                    await send_alert(voice_channel.guild, *e.args)

            # add member to db
            channel_member: DynChannelMember | None = await db.get(
                DynChannelMember, member_id=member.id, channel_id=voice_channel.id
            )
            if not channel_member:
                dyn_channel.members.append(channel_member := await DynChannelMember.create(member.id, voice_channel.id))

            # fix owner
            owner: DynChannelMember | None = await db.get(DynChannelMember, id=dyn_channel.owner_id)
            update_owner = False
            if (not owner or channel_member.timestamp < owner.timestamp) and dyn_channel.owner_id != channel_member.id:
                if not member.bot:
                    dyn_channel.owner_id = channel_member.id
                    update_owner = True
            if update_owner or dyn_channel.owner_override == member.id:
                await self.cache_owner(dyn_channel, await self.fetch_owner_from_db(dyn_channel))

    async def member_leave(self, member: Member, voice_channel: VoiceChannel):
        async with channel_locks[voice_channel.id]:
            dyn_channel: DynChannel | None = await DynChannel.get(channel_id=voice_channel.id)
            if not dyn_channel:
                return

            if not dyn_channel.locked:
                text_channel: TextChannel | None = self.bot.get_channel(dyn_channel.text_id)
                if text_channel:
                    try:
                        await text_channel.edit(
                            overwrites=merge_permission_overwrites(
                                text_channel.overwrites,
                                (
                                    member,
                                    PermissionOverwrite(read_messages=None, send_messages=None, add_reactions=None),
                                ),
                            )
                        )
                    except Forbidden:
                        await send_alert(voice_channel.guild, t.could_not_overwrite_permissions(text_channel.mention))
                await voice_channel.edit(
                    overwrites=merge_permission_overwrites(
                        voice_channel.overwrites,
                        (member, PermissionOverwrite(read_messages=None, send_messages=None, add_reactions=None)),
                    )
                )
            await self.send_voice_msg(dyn_channel, t.voice_channel, [t.dyn_voice_left(member.mention)])

            owner: DynChannelMember | None = await db.get(DynChannelMember, id=dyn_channel.owner_id)
            if owner and owner.member_id == member.id or dyn_channel.owner_override == member.id:
                await self.fix_owner(dyn_channel)

            if any(not m.bot for m in voice_channel.members):
                return

            async def delete_text():
                if text_channel := self.bot.get_channel(dyn_channel.text_id):
                    try:
                        await text_channel.delete()
                    except Forbidden:
                        await send_alert(text_channel.guild, t.could_not_delete_channel(text_channel.mention))
                        return

            async def delete_voice():
                dyn_channel.owner_id = None
                dyn_channel.owner_override = None
                await db.exec(delete(DynChannelMember).filter_by(channel_id=voice_channel.id))
                dyn_channel.members.clear()

                try:
                    await voice_channel.delete()
                except Forbidden:
                    await send_alert(voice_channel.guild, t.could_not_delete_channel(voice_channel.mention))
                    return
                else:
                    await db.delete(dyn_channel)

            async def create_new_channel() -> bool:
                # check if there is at least one empty channel
                if not all(
                    any(not m.bot for m in c.members)
                    for chnl in dyn_channel.group.channels
                    if chnl.channel_id != dyn_channel.channel_id and (c := self.bot.get_channel(chnl.channel_id))
                ):
                    return True

                category: CategoryChannel | Guild = voice_channel.category or voice_channel.guild
                if len(category.channels) >= 50:
                    await send_alert(voice_channel.guild, t.could_not_create_voice_channel(t.category_full))
                    return False

                guild: Guild = voice_channel.guild
                category: CategoryChannel | Guild = voice_channel.category or guild

                overwrites = voice_channel.overwrites
                if dyn_channel.locked:
                    overwrites = remove_lock_overrides(
                        dyn_channel, voice_channel, overwrites, keep_members=False, reset_user_role=True
                    )
                try:
                    new_channel = await safe_create_voice_channel(
                        category, dyn_channel, await self.get_channel_name(guild), overwrites
                    )
                except (Forbidden, HTTPException):
                    await send_alert(guild, t.could_not_create_voice_channel)
                    return False
                else:
                    await DynChannel.create(new_channel.id, dyn_channel.group_id)
                    return True

            await delete_text()
            if await create_new_channel():
                await delete_voice()

    async def on_voice_state_update(self, member: Member, before: VoiceState, after: VoiceState):
        if before.channel == after.channel:
            return

        async def delayed(delay, key, func, delay_callback, *args):
            await asyncio.sleep(delay)
            delay_callback()
            async with channel_locks[key]:
                async with db_context():
                    return await func(*args)

        async def create_task(delay, c, task_dict, cancel_dict, func):
            dyn_channel: DynChannel | None = await DynChannel.get(channel_id=channel.id)
            if not dyn_channel:
                return

            await collect_links(member.guild, roles := set(), dyn_channel.group_id)
            if func == self.member_leave:
                await update_roles(member, remove=roles)
            else:
                await update_roles(member, add=roles)

            key = member, c
            if task := cancel_dict.pop(key, None):
                task.cancel()
            elif key not in task_dict:
                task_dict[key] = asyncio.create_task(
                    delayed(delay, dyn_channel.channel_id, func, lambda: task_dict.pop(key, None), *key)
                )

        remove: set[Role] = set()
        add: set[Role] = set()

        if channel := before.channel:
            await collect_links(channel.guild, remove, str(channel.id))
            if (k := (member, channel)) in self._recent_kicks:
                self._recent_kicks.remove(k)
                await self.member_leave(member, channel)
            else:
                await create_task(5, channel, self._leave_tasks, self._join_tasks, self.member_leave)

        if channel := after.channel:
            await collect_links(channel.guild, add, str(channel.id))
            await create_task(1, channel, self._join_tasks, self._leave_tasks, self.member_join)

        await update_roles(member, add=add, remove=remove)

    @commands.group(aliases=["vc"])
    @guild_only()
    @docs(t.commands.voice)
    async def voice(self, ctx: Context):
        if ctx.invoked_subcommand is None:
            raise UserInputError

    @voice.group(name="dynamic", aliases=["dyn", "d"])
    @VoiceChannelPermission.dyn_read.check
    @docs(t.commands.voice_dynamic)
    async def voice_dynamic(self, ctx: Context):
        if len(ctx.message.content.lstrip(ctx.prefix).split()) > 2:
            if ctx.invoked_subcommand is None:
                raise UserInputError
            return

        embed = Embed(title=t.voice_channel, colour=Colors.Voice)

        group: DynGroup
        group_data = []
        async for group in await db.stream(select(DynGroup, DynGroup.channels)):
            idx = 0
            channels: list[tuple[str, VoiceChannel, TextChannel | None]] = []
            for channel in group.channels:
                voice_channel: VoiceChannel | None = ctx.guild.get_channel(channel.channel_id)
                text_channel: TextChannel | None = ctx.guild.get_channel(channel.text_id)
                if not voice_channel:
                    await db.delete(channel)
                    continue
                idx = voice_channel.category.position

                if channel.locked:
                    if voice_channel.overwrites_for(voice_channel.guild.get_role(channel.group.user_role)).view_channel:
                        icon = "lock"
                    else:
                        icon = "man_detective"
                else:
                    icon = "unlock"

                channels.append((icon, voice_channel, text_channel))

            if not channels:
                await db.delete(group)
                continue

            group_data.append((idx, group, channels))

        for data in sorted(group_data):
            _, group, channels = data
            embed.add_field(
                name=t.cnt_channels(":memo:" if group.text_channel_by_default else "", cnt=len(channels)),
                value="\n".join(f":{icon}: {vc.mention} {txt.mention if txt else ''}" for icon, vc, txt in channels),
                inline=False,
            )

        if not embed.fields:
            embed.colour = Colors.error
            embed.description = t.no_dyn_group
        else:
            embed.set_footer(text=t.memo_meaning(name_to_emoji["memo"]))
        await send_long_embed(ctx, embed, paginate=True)

    @voice_dynamic.command(name="require_whitespaces", aliases=["rw"])
    @VoiceChannelPermission.dyn_write.check
    async def require_whitespaces(self, ctx: Context, enabled: bool):
        """
        enable/disable the requirement to separate the parts of a voice channel name by whitespaces
        """

        embed = Embed(title=t.voice_channel, colour=Colors.Voice)
        await DynamicVoiceSettings.require_whitespaces.set(enabled)
        if enabled:
            embed.description = t.whitespaces_now_required
            await send_to_changelog(ctx.guild, t.log_whitespaces_now_required)
        else:
            embed.description = t.whitespaces_no_longer_required
            await send_to_changelog(ctx.guild, t.log_whitespaces_no_longer_required)
        await reply(ctx, embed=embed)

    @voice_dynamic.command(name="add", aliases=["a", "+"])
    @VoiceChannelPermission.dyn_write.check
    @docs(t.commands.voice_dynamic_add)
    async def voice_dynamic_add(
        self, ctx: Context, user_role: Role | None, create_text_channel_by_default: bool, *, voice_channel: VoiceChannel
    ):
        async with channel_locks[voice_channel.id]:
            everyone = voice_channel.guild.default_role
            user_role = user_role or everyone
            if not check_voice_permissions(voice_channel, user_role):
                raise CommandError(t.invalid_user_role(user_role.mention if user_role != everyone else "@everyone"))

            if await db.exists(filter_by(DynChannel, channel_id=voice_channel.id)):
                raise CommandError(t.dyn_group_already_exists)

            try:
                await voice_channel.edit(name=await self.get_channel_name(voice_channel.guild))
            except Forbidden:
                raise CommandError(t.cannot_edit)

            await DynGroup.create(voice_channel.id, user_role.id, create_text_channel_by_default)
            embed = Embed(title=t.voice_channel, colour=Colors.Voice, description=t.dyn_group_created)
            await reply(ctx, embed=embed)
            await send_to_changelog(
                ctx.guild,
                t.log_dyn_group_created(
                    t.default_text_channels_active
                    if create_text_channel_by_default
                    else t.default_text_channels_not_active
                ),
            )

    @voice_dynamic.command(name="remove", aliases=["del", "d", "r", "-"])
    @VoiceChannelPermission.dyn_write.check
    @docs(t.commands.voice_dynamic_remove)
    async def voice_dynamic_remove(self, ctx: Context, *, voice_channel: VoiceChannel):
        channel: DynChannel | None = await db.get(
            DynChannel, [DynChannel.group, DynGroup.channels], DynChannel.members, channel_id=voice_channel.id
        )
        if not channel:
            raise CommandError(t.dyn_group_not_found)

        for c in channel.group.channels:
            if (x := self.bot.get_channel(c.channel_id)) and c.channel_id != voice_channel.id:
                try:
                    await x.delete()
                except Forbidden:
                    raise CommandError(t.could_not_delete_channel(x.mention))
            if x := self.bot.get_channel(c.text_id):
                try:
                    await x.delete()
                except Forbidden:
                    raise CommandError(t.could_not_delete_channel(x.mention))

        await db.delete(channel.group)
        embed = Embed(title=t.voice_channel, colour=Colors.Voice, description=t.dyn_group_removed)
        await reply(ctx, embed=embed)
        await send_to_changelog(ctx.guild, t.log_dyn_group_removed)

    @voice_dynamic.group(name="edit", aliases=["e"])
    @VoiceChannelPermission.dyn_write.check
    @docs(t.commands.voice_dynamic_edit)
    async def voice_dynamic_edit(self, ctx: Context):
        if ctx.invoked_subcommand is None:
            raise UserInputError

    @voice_dynamic_edit.command(name="default_text_channel")
    @VoiceChannelPermission.dyn_write.check
    @docs(t.commands.edit_default_text_channel)
    async def set_text_channel_default_mode(self, ctx: Context, active: bool, voice_channel: VoiceChannel):
        channel: DynChannel | None = await db.get(
            DynChannel, [DynChannel.group, DynGroup.channels], DynChannel.members, channel_id=voice_channel.id
        )
        if not channel:
            raise CommandError(t.dyn_group_not_found)

        channel.group.text_channel_by_default = active
        embed = Embed(title=t.voice_channel, colour=Colors.Voice, description=t.dyn_group_edited)
        await reply(ctx, embed=embed)
        await send_to_changelog(
            ctx.guild,
            t.log_dyn_group_edited(t.default_text_channels_active if active else t.default_text_channels_not_active),
        )

    @voice.command(name="help", aliases=["commands", "c"])
    @docs(t.commands.help)
    async def voice_help(self, ctx: Context):
        message = await reply(ctx, embed=await get_commands_embed())

        if channel := await DynChannel.get(text_id=ctx.channel.id):
            await self.update_control_message(channel, message)
        if channel := await DynChannel.get(channel_id=ctx.channel.id):
            await self.update_control_message(channel, message)

    @voice.command(name="info", aliases=["i"])
    @docs(t.commands.voice_info)
    async def voice_info(self, ctx: Context, *, voice_channel: VoiceChannel | Member | None = None):
        if not voice_channel:
            if channel := await db.get(DynChannel, channel_id=ctx.channel.id):
                voice_channel = self.bot.get_channel(channel.channel_id)
            if not channel:
                if channel := await db.get(DynChannel, text_id=ctx.channel.id):
                    voice_channel = self.bot.get_channel(channel.channel_id)

        if not isinstance(voice_channel, VoiceChannel):  # no user given and voice channel not found yet
            member = voice_channel or ctx.author
            if not member.voice:  # given member or author is not in voice
                if not voice_channel:  # author not in voice
                    raise CommandError(t.not_in_voice)
                if await self.is_teamler(ctx.author):  # given member not in voice and we are teamler
                    raise CommandError(t.user_not_in_voice)
                raise CommandError(tg.permission_denied)  # given member not in voice and we are not teamler
            voice_channel = member.voice.channel

        channel: DynChannel | None = await db.get(
            DynChannel, [DynChannel.group, DynGroup.channels], DynChannel.members, channel_id=voice_channel.id
        )
        if not channel:
            raise CommandError(t.dyn_group_not_found)

        if not voice_channel.permissions_for(ctx.author).connect:
            raise CommandError(tg.permission_denied)

        await self.send_voice_info(ctx, channel)

    async def send_voice_info(self, target: Messageable | InteractionResponse, dyn_channel: DynChannel):
        voice_channel: VoiceChannel = self.bot.get_channel(dyn_channel.channel_id)
        if dyn_channel.locked:
            if voice_channel.overwrites_for(voice_channel.guild.get_role(dyn_channel.group.user_role)).view_channel:
                state = t.state.locked
            else:
                state = t.state.hidden
        else:
            state = t.state.unlocked
        ping = t.ping.no_ping if dyn_channel.no_ping else t.ping.ping

        embed = Embed(title=t.voice_info, color=[Colors.unlocked, Colors.locked][dyn_channel.locked])
        embed.add_field(name=t.voice_name, value=voice_channel.name)
        embed.add_field(name=t.voice_state, value=state)

        if owner := await self.get_owner_from_cache(dyn_channel):
            embed.add_field(name=t.voice_owner, value=owner.mention)
        embed.add_field(name=t.voice_ping, value=ping)

        out = []

        active = members = set(voice_channel.members)
        if dyn_channel.locked:
            members = {m for m in voice_channel.overwrites if isinstance(m, Member)}

        join_map = {m.member_id: m.timestamp.timestamp() for m in dyn_channel.members}
        members = sorted(
            members, key=lambda m: -1 if m.id == dyn_channel.owner_override else join_map.get(m.id, 1e1337)
        )

        for member in members:
            if member in active:
                out.append(f":small_orange_diamond: {member.mention}")
            else:
                out.append(f":small_blue_diamond: {member.mention}")

        if dyn_channel.locked:
            name = t.voice_members.locked(len(active), cnt=len(members))
        else:
            name = t.voice_members.unlocked(cnt=len(members))
        embed.add_field(name=name, value="\n".join(out), inline=False)

        out = []
        for m, p in voice_channel.overwrites.items():
            if not p.connect and isinstance(m, Member):
                out.append(f":x: {m.mention}")
        if out:
            embed.add_field(name=t.voice_members.blacklisted(cnt=len(out)), value="\n".join(out), inline=False)

        messages = await send_long_embed(target, embed, paginate=True)
        if isinstance(target, InteractionResponse):
            return
        await self.update_control_message(dyn_channel, messages[-1])

    @voice.command(name="rename")
    @optional_permissions(VoiceChannelPermission.dyn_rename, VoiceChannelPermission.override_owner)
    @docs(t.commands.voice_rename)
    async def voice_rename(self, ctx: Context, *, name: str | None):
        channel, voice_channel, text_channel = await self.get_channel(ctx.author, check_owner=True)
        old_name = voice_channel.name

        if not name:
            name = await self.get_channel_name(ctx.guild)
        elif not self.check_name(name, False, await DynamicVoiceSettings.require_whitespaces.get())[0]:
            if not await VoiceChannelPermission.dyn_rename.check_permissions(ctx.author):
                raise CommandError(t.no_custom_name(prefix=await get_prefix()))

        if any(c.id != voice_channel.id and name == c.name for c in voice_channel.guild.voice_channels):
            if not await Confirmation().run(ctx, t.rename_description):
                return

        try:
            await rename_channel(voice_channel, name)
            if text_channel:
                await rename_channel(text_channel, name)
        except Forbidden:
            raise CommandError(t.cannot_edit)
        except HTTPException:
            raise CommandError(t.rename_failed)

        await self.send_voice_msg(channel, t.voice_channel, [t.renamed(ctx.author.mention, old_name, name)])
        await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @voice.command(name="create_text_channel", aliases=["ctc", "tc"])
    @optional_permissions(VoiceChannelPermission.override_owner)
    @docs(t.commands.voice_create_text_channel)
    async def create_text_channel_command(self, ctx: Context):
        channel, voice_channel, text_channel = await self.get_channel(ctx.author, check_owner=True)
        async with channel_locks[channel.channel_id]:
            if text_channel:
                raise CommandError(t.text_channel_exists(text_channel.mention))
            await self.create_text_channel(channel, voice_channel, ctx)
            await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @voice.command(name="owner", aliases=["o"])
    @optional_permissions(VoiceChannelPermission.override_owner)
    @docs(t.commands.voice_owner)
    async def voice_owner(self, ctx: Context, member: Member):
        channel, voice_channel, _ = await self.get_channel(ctx.author, check_owner=True)

        async with channel_locks[channel.channel_id]:
            if member not in voice_channel.members:
                raise CommandError(t.user_not_in_this_channel)
            if member.bot:
                raise CommandError(t.bot_no_owner_transfer)

            if await self.get_owner_from_cache(channel) == member:
                raise CommandError(t.already_owner(member.mention))

            channel.owner_override = member.id
            await self.cache_owner(channel, member)
            await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @voice.command(name="lock", aliases=["l"])
    @optional_permissions(VoiceChannelPermission.override_owner)
    @docs(t.commands.voice_lock)
    async def voice_lock(self, ctx: Context):
        channel, voice_channel, _ = await self.get_channel(ctx.author, check_owner=True)

        async with channel_locks[channel.channel_id]:
            if channel.locked:
                raise CommandError(t.already_locked)

            await self.lock_channel(ctx.author, channel, voice_channel, hide=False)
            await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @voice.command(name="hide", aliases=["h"])
    @optional_permissions(VoiceChannelPermission.override_owner)
    @docs(t.commands.voice_hide)
    async def voice_hide(self, ctx: Context):
        channel, voice_channel, _ = await self.get_channel(ctx.author, check_owner=True)

        async with channel_locks[channel.channel_id]:
            user_role = voice_channel.guild.get_role(channel.group.user_role)
            locked = channel.locked
            if locked and not voice_channel.overwrites_for(user_role).view_channel:
                raise CommandError(t.already_hidden)

            await self.lock_channel(ctx.author, channel, voice_channel, hide=True)
            await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @voice.command(name="show", aliases=["s", "unhide"])
    @optional_permissions(VoiceChannelPermission.override_owner)
    @docs(t.commands.voice_show)
    async def voice_show(self, ctx: Context):
        channel, voice_channel, _ = await self.get_channel(ctx.author, check_owner=True)
        async with channel_locks[channel.channel_id]:
            user_role = voice_channel.guild.get_role(channel.group.user_role)
            if not channel.locked or voice_channel.overwrites_for(user_role).view_channel:
                raise CommandError(t.not_hidden)

            await self.unhide_channel(ctx.author, channel, voice_channel)
            await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @voice.command(name="unlock", aliases=["u"])
    @optional_permissions(VoiceChannelPermission.override_owner)
    @docs(t.commands.voice_unlock)
    async def voice_unlock(self, ctx: Context):
        channel, voice_channel, _ = await self.get_channel(ctx.author, check_owner=True)
        async with channel_locks[channel.channel_id]:
            if not channel.locked:
                raise CommandError(t.already_unlocked)

            await self.unlock_channel(ctx.author, channel, voice_channel)
            await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @voice.command(name="no_ping", aliases=["np", "dp", "fp"])
    @optional_permissions(VoiceChannelPermission.override_owner)
    @docs(t.commands.disable_ping)
    async def disable_ping(self, ctx: Context):
        channel, voice_channel, _ = await self.get_channel(ctx.author, check_owner=True)
        async with channel_locks[channel.channel_id]:
            if channel.no_ping:
                raise CommandError(t.pings_already_inactive)

            await self.change_channel_ping(ctx.author, channel, no_ping=True)
            await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @voice.command(name="ping", aliases=["p", "ep", "ap"])
    @optional_permissions(VoiceChannelPermission.override_owner)
    @docs(t.commands.enable_ping)
    async def enable_ping(self, ctx: Context):
        channel, voice_channel, _ = await self.get_channel(ctx.author, check_owner=True)
        async with channel_locks[channel.channel_id]:
            if not channel.no_ping:
                raise CommandError(t.pings_already_active)

            await self.change_channel_ping(ctx.author, channel, no_ping=False)
            await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @voice.command(name="join_request", aliases=["j", "jr"])
    async def join_request(self, ctx: Context, channel: DynamicVoiceConverter):
        """
        send a join request for a voice channel

        you can use
        - the channel id of voice- or text channel
        - a link ("copy link")
        - or the name (needs to ble enclosed in quotes if it contains whitespaces)
        to select the channel
        """
        channel: TextChannel | VoiceChannel
        dyn_channel, voice_channel, _ = await self.get_channel(
            ctx.author, check_locked=True, check_owner=False, channel=channel
        )
        if ctx.author.voice and ctx.author.voice.channel.id == channel.id:
            raise CommandError(t.already_in_channel)
        if ctx.author in voice_channel.overwrites:
            raise CommandError(t.already_whitelisted)

        if ctx.author.id in join_requests.locks:
            raise CommandError(t.too_many_requests)
        await self.request_join(ctx, dyn_channel, voice_channel)

    @run_as_task
    @db_wrapper
    async def request_join(self, ctx: Context, dyn_channel: DynChannel, voice_channel: VoiceChannel):
        async with join_requests[ctx.author.id]:
            await ctx.message.add_reaction(name_to_emoji["postal_horn"])
            owner = await self.get_owner_from_cache(dyn_channel)
            if dyn_channel.no_ping:
                add = await Confirmation(user=owner, delete_after_confirm=None, timeout=120).run(
                    voice_channel, t.join_request(owner.mention, ctx.author.mention, ctx.author.id)
                )
            else:
                add = await Confirmation(user=owner, delete_after_confirm=None, timeout=120).run(
                    voice_channel,
                    t.join_request(owner.mention, ctx.author.mention, ctx.author.id),
                    content=owner.mention,
                )
            try:
                if add:
                    await self.add_to_channel(dyn_channel, voice_channel, [ctx.author])
                    await ctx.reply(t.request_approved)
                else:
                    await ctx.reply(t.request_denied)
            except HTTPException:
                pass

    @voice.command(name="add", aliases=["a", "+", "invite"])
    @optional_permissions(VoiceChannelPermission.override_owner)
    @docs(t.commands.voice_add)
    async def voice_add(self, ctx: Context, *members: Greedy[Member]):
        members: set[Member] = set(members)
        if not members:
            raise UserInputError

        channel, voice_channel, _ = await self.get_channel(ctx.author, check_owner=True, check_locked=False)

        async with channel_locks[channel.channel_id]:
            if self.bot.user in members:
                raise CommandError(t.cannot_add_user(self.bot.user.mention))

            await self.add_to_channel(channel, voice_channel, list(members))

            await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @voice.command(name="remove", aliases=["r", "-", "kick", "k", "blacklist", "bl"])
    @optional_permissions(VoiceChannelPermission.override_owner)
    @docs(t.commands.voice_remove)
    async def voice_remove(self, ctx: Context, *members: Greedy[Member]):
        members: set[Member] = set(members)
        if not members:
            raise UserInputError

        channel, voice_channel, _ = await self.get_channel(ctx.author, check_owner=True, check_locked=False)

        async with channel_locks[channel.channel_id]:
            if self.bot.user in members:
                raise CommandError(t.cannot_remove_user(self.bot.user.mention))
            if ctx.author in members:
                raise CommandError(t.cannot_remove_user(ctx.author.mention))

            team_roles: list[Role] = [
                team_role
                for role_name in self.team_roles
                if (team_role := ctx.guild.get_role(await RoleSettings.get(role_name)))
                if check_voice_permissions(voice_channel, team_role)
            ]
            for member in members:
                # if member not in voice_channel.overwrites:
                #     raise CommandError(t.not_added(member.mention))
                if any(role in member.roles for role in team_roles):
                    raise CommandError(t.cannot_remove_user(member.mention))

            await self.remove_from_channel(channel, voice_channel, list(members))

            await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @voice.group(name="role_links", aliases=["rl"])
    @VoiceChannelPermission.link_read.check
    @docs(t.commands.voice_link)
    async def voice_link(self, ctx: Context):
        if len(ctx.message.content.lstrip(ctx.prefix).split()) > 2:
            if ctx.invoked_subcommand is None:
                raise UserInputError
            return

        guild: Guild = ctx.guild

        out: list[tuple[VoiceChannel, Role]] = []
        link: RoleVoiceLink
        async for link in await db.stream(select(RoleVoiceLink)):
            role: Role | None = guild.get_role(link.role)
            if role is None:
                await db.delete(link)
                continue

            if link.voice_channel.isnumeric():
                voice: VoiceChannel | None = guild.get_channel(int(link.voice_channel))
                if not voice:
                    await db.delete(link)
                    continue
                out.append((voice, role))
            else:
                group: DynGroup | None = await db.get(DynGroup, DynGroup.channels, id=link.voice_channel)
                if not group:
                    await db.delete(link)
                    continue

                for channel in group.channels:
                    if voice := guild.get_channel(channel.channel_id):
                        out.append((voice, role))

        embed = Embed(title=t.voice_channel, color=Colors.Voice)
        embed.description = "\n".join(
            f"{voice.mention} (`{voice.id}`) -> {role.mention} (`{role.id}`)" for voice, role in out
        )

        if not out:
            embed.colour = Colors.error
            embed.description = t.no_links_created

        await send_long_embed(ctx, embed)

    def gather_members(self, channel: DynChannel | None, voice_channel: VoiceChannel) -> set[Member]:
        members: set[Member] = set(voice_channel.members)
        if not channel:
            return members

        for dyn_channel in channel.group.channels:
            if x := self.bot.get_channel(dyn_channel.channel_id):
                members.update(x.members)

        return members

    @voice_link.command(name="add", aliases=["a", "+"])
    @VoiceChannelPermission.link_write.check
    @docs(t.commands.voice_link_add)
    async def voice_link_add(self, ctx: Context, voice_channel: VoiceChannel, *, role: Role):
        if channel := await DynChannel.get(channel_id=voice_channel.id):
            voice_id = channel.group_id
        else:
            voice_id = str(voice_channel.id)

        if await db.exists(filter_by(RoleVoiceLink, role=role.id, voice_channel=voice_id)):
            raise CommandError(t.link_already_exists)

        check_role_assignable(role)

        await RoleVoiceLink.create(role.id, voice_id)

        for m in self.gather_members(channel, voice_channel):
            asyncio.create_task(update_roles(m, add={role}))

        embed = Embed(title=t.voice_channel, colour=Colors.Voice, description=t.link_created(voice_channel, role.id))
        await reply(ctx, embed=embed)
        await send_to_changelog(ctx.guild, t.log_link_created(voice_channel, role))

    @voice_link.command(name="remove", aliases=["del", "r", "d", "-"])
    @VoiceChannelPermission.link_write.check
    @docs(t.commands.voice_link_remove)
    async def voice_link_remove(self, ctx: Context, voice_channel: VoiceChannel, *, role: Role):
        if channel := await DynChannel.get(channel_id=voice_channel.id):
            voice_id = channel.group_id
        else:
            voice_id = str(voice_channel.id)

        link: RoleVoiceLink | None = await db.get(RoleVoiceLink, role=role.id, voice_channel=voice_id)
        if not link:
            raise CommandError(t.link_not_found)

        await db.delete(link)

        for m in self.gather_members(channel, voice_channel):
            asyncio.create_task(update_roles(m, remove={role}))

        embed = Embed(title=t.voice_channel, colour=Colors.Voice, description=t.link_deleted)
        await reply(ctx, embed=embed)
        await send_to_changelog(ctx.guild, t.log_link_deleted(voice_channel, role))

    @voice.group(aliases=["wl"])
    @guild_only()
    @VoiceChannelPermission.dyn_whitelist_read.check
    @docs(t.commands.whitelist)
    async def whitelist(self, ctx: Context):
        if ctx.invoked_subcommand is None:
            raise UserInputError

    @whitelist.command(name="test", aliases=["check", "t", "c"])
    @VoiceChannelPermission.dyn_whitelist_check.check
    async def whitelist_check(self, ctx: Context, *, name: str):
        """
        check a name for whitelist conformity

        This command checks if the given name is an allowed name for a dynamic voice channel.
        Depending on the target voice channel, you might need to separate the pars of this name by whitespaces.
        This command will ignore this requirement entirely.
        """
        ok, all_name_fragments = self.check_name(name, False, False)
        embed = Embed(colour=Colors.Voice, title=t.voice_channel)
        if not ok:
            embed.description = t.name_no_matches(prefix=await get_prefix())
        else:
            embed.description = t.name_matches(cnt=len(all_name_fragments))
        await ctx.reply(embed=embed)

    @whitelist.command(name="test_recursive", aliases=["check_recursive", "tr", "cr"])
    @VoiceChannelPermission.dyn_whitelist_check_parts.check
    async def whitelist_check_recursive(self, ctx: Context, *, name: str):
        """
        check a name for whitelist conformity and list all parts

        This command checks if the given name is an allowed name for a dynamic voice channel.
        Depending on the target voice channel, you might need to separate the pars of this name by whitespaces.
        This command will ignore this requirement entirely.
        """
        ok, all_name_fragments = self.check_name(name, True, False)
        embed = Embed(colour=Colors.Voice, title=t.voice_channel)
        if not ok:
            embed.description = t.name_no_matches
            await ctx.reply(embed=embed)
        else:
            embed.description = t.name_matches_parts(cnt=len(all_name_fragments))
            embed.set_footer(text=t.parts_footer_custom(prefix=await get_prefix()))
            for i, combination in enumerate(all_name_fragments):
                embed.add_field(
                    name=t.name_match_combination(i + 1),
                    value="\n".join(map(lambda x: f"{x[0]}: {escape_codeblock(x[1])}", combination)),
                )
            await send_long_embed(ctx, embed, paginate=True, repeat_footer=True, repeat_title=True)

    @whitelist.command(name="list", aliases=["l", "s"])
    @VoiceChannelPermission.dyn_whitelist_list.check
    async def list(self, ctx: Context):
        """
        List all allowed phrases for this server

        Show all phrases which can be used for renaming dynamic voice channels.
        Will not list phrases, which are on lists in files.
        """
        embed = Embed(
            title=t.voice_channel,
            colour=Colors.Voice,
            description=t.phrases_list(", ".join(map(escape_codeblock, sorted(self.custom_names))))
            if self.custom_names
            else t.no_phrases_allowed,
        )
        embed.set_footer(text=t.phrases_list_footer)
        await send_long_embed(ctx, embed=embed)

    @whitelist.command(name="add", aliases=["a", "+"])
    @VoiceChannelPermission.dyn_whitelist_write.check
    async def add(self, ctx: Context, *, name: str):
        """
        Allow ONE phrase for renaming custom voice channels (whitespaces are usable characters!)

        Allow a phrase for renaming dynamic voice channels.
        Phrases are case-insensitive.
        Phrases are unique.
        """
        name = name.strip().lower()
        if len(name) > 25:
            raise CommandError(t.phrase_too_long)
        if await db.exists(filter_by(AllowedChannelName, name=name.lower())):
            raise CommandError(t.phrase_exists)
        self.custom_names.add(name)
        await AllowedChannelName.create(name)
        embed = Embed(
            title=t.voice_channel, colour=Colors.Voice, description=t.phrase_whitelisted(escape_codeblock(name))
        )
        await reply(ctx, embed=embed)
        await send_to_changelog(ctx.guild, t.log_phrase_whitelisted(escape_codeblock(name)))

    @whitelist.command(name="remove", aliases=["del", "r", "d", "-"])
    @VoiceChannelPermission.dyn_whitelist_write.check
    async def remove(self, ctx: Context, *, name: str):
        """
        Remove a phrase for renaming custom voice channels

        Remove a phrase for renaming dynamic voice channels.
        Phrases are case-insensitive.
        Phrases are unique.
        The phrase will be sanitized; all unusable characters will be removed.
        """
        name = name.strip().lower()
        if not (item := await db.get(AllowedChannelName, name=name.lower())):
            raise CommandError(t.phrase_not_existing)
        self.custom_names.remove(name)
        await db.delete(item)
        embed = Embed(
            title=t.voice_channel, colour=Colors.Voice, description=t.phrase_whitelist_removed(escape_codeblock(name))
        )
        await reply(ctx, embed=embed)
        await send_to_changelog(ctx.guild, t.log_phrase_whitelist_removed(escape_codeblock(name)))


"""
vc owner nach zeit
nach x stunden wird man aus der schlange und kette geworfen

ownerkette:
ersteller der kette und erster in der kette haben ownerrechte
alle anderen haben + und - rechte
vc owner - list owners
+ add user
- remove user
"""
