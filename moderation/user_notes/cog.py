from datetime import datetime
from typing import Optional

from discord import Embed, Member, Option, SlashCommandGroup, User
from discord.commands import ApplicationContext
from discord.ext.commands import CommandError
from discord.utils import format_dt

from PyDrocsid.cog import Cog
from PyDrocsid.command import Confirmation, reply
from PyDrocsid.database import db, select
from PyDrocsid.embeds import send_long_embed
from PyDrocsid.translations import t
from PyDrocsid.util import is_teamler

from .colors import Colors
from .models import UserNote
from .permissions import UserNotePermission
from ...contributor import Contributor
from ...pubsub import get_userlog_entries, send_to_changelog


tg = t.g
t = t.user_notes


class UserNoteCog(Cog, name="User Notes"):
    CONTRIBUTORS = [Contributor.Florian, Contributor.Defelo]

    @get_userlog_entries.subscribe
    async def handle_get_userlog_entries(self, user_id: int, author: Member) -> list[tuple[datetime, str]]:
        if not await is_teamler(author):
            return []

        out: list[tuple[datetime, str]] = []

        note: UserNote
        async for note in await db.stream(select(UserNote).filter_by(member_id=user_id)):
            out.append(
                (note.timestamp, t.ulog_entry(f"<@{note.author_id}>", "\n" * ("\n" in note.content) + note.content))
            )

        return out

    user_notes = SlashCommandGroup(
        name="notes",
        description="Notizen über Benutzer verwalten",
        guild_only=True,
    )

    @user_notes.command(name="show", description="Notizen zu einem Benutzer anzeigen")
    @UserNotePermission.read.check
    async def user_notes_show(self, ctx: ApplicationContext, user: User = Option(User, description="Benutzer")):
        embed = Embed(title=t.user_notes, colour=Colors.user_notes)
        embed.set_author(name=f"{user} ({user.id})", icon_url=user.display_avatar.url)
        note: UserNote
        async for note in await db.stream(select(UserNote).filter_by(member_id=user.id)):
            embed.add_field(
                name=format_dt(note.timestamp, style="D") + " " + format_dt(note.timestamp, style="T"),
                value=t.user_note_entry(id=note.id, author=f"<@{note.author_id}>", content=note.content),
                inline=False,
            )

        if not embed.fields:
            embed.colour = Colors.error
            embed.description = t.no_notes

        await send_long_embed(ctx, embed, paginate=True)

    @user_notes.command(name="add", description="Notiz zu einem Benutzer hinzufügen")
    @UserNotePermission.write.check
    async def user_notes_add(
        self,
        ctx: ApplicationContext,
        user: User = Option(User, description="Benutzer"),
        content: str = Option(str, description="Inhalt der Notiz"),
    ):
        if len(content) > 1000:
            raise CommandError(t.too_long)

        await UserNote.create(user.id, ctx.author.id, content)
        await send_to_changelog(ctx.guild, t.new_note(ctx.author.mention, user.mention, content))
        await reply(ctx, content=f":white_check_mark: Notiz für {user.mention} gespeichert.")

    @user_notes.command(name="remove", description="Notiz anhand der ID löschen")
    @UserNotePermission.write.check
    async def user_notes_remove(self, ctx: ApplicationContext, note_id: int = Option(int, description="Notiz-ID")):
        user_note: Optional[UserNote] = await db.get(UserNote, id=note_id)
        if not user_note:
            raise CommandError(t.note_not_found)

        if not await Confirmation().run(ctx, t.confirm(f"<@{user_note.member_id}>", user_note.content)):
            return

        await db.delete(user_note)
        await send_to_changelog(
            ctx.guild, t.removed_note(ctx.author.mention, f"<@{user_note.member_id}>", user_note.content)
        )
        await reply(ctx, content=f":white_check_mark: Notiz #{note_id} gelöscht.")
