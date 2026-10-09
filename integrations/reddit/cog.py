import re
from datetime import datetime
from typing import List, Optional

from aiohttp import ClientSession
from discord import Embed, Option, SlashCommandGroup, TextChannel, Thread
from discord.commands import ApplicationContext
from discord.ext import tasks
from discord.ext.commands import CommandError

from PyDrocsid.cog import Cog
from PyDrocsid.command import reply
from PyDrocsid.config import Config
from PyDrocsid.database import db, db_wrapper, filter_by, select
from PyDrocsid.logger import get_logger
from PyDrocsid.translations import t
from PyDrocsid.util import check_message_send_permissions

from .colors import Colors
from .models import RedditChannel, RedditPost
from .permissions import RedditPermission
from .settings import RedditSettings
from ...contributor import Contributor
from ...pubsub import send_alert, send_to_changelog


tg = t.g
t = t.reddit

logger = get_logger(__name__)


def remove_prefix(subreddit: str) -> str:
    return re.sub("^(/r/|r/)", "", subreddit)


async def get_subreddit_name(subreddit: str) -> str | None:
    subreddit = remove_prefix(subreddit)
    async with ClientSession() as session, session.get(
        # raw_json=1 as parameter to get unicode characters instead of html escape sequences
        f"https://www.reddit.com/r/{subreddit}/about.json?raw_json=1",
        headers={"User-agent": f"{Config.NAME}/{Config.VERSION}"},
        allow_redirects=False,
    ) as response:
        if response.status != 200:
            return None

        return (await response.json())["data"]["display_name"]


async def fetch_reddit_posts(subreddit: str, limit: int) -> Optional[List[dict]]:
    subreddit = remove_prefix(subreddit)
    async with ClientSession() as session, session.get(
        # raw_json=1 as parameter to get unicode characters instead of html escape sequences
        f"https://www.reddit.com/r/{subreddit}/hot.json?raw_json=1",
        headers={"User-agent": f"{Config.NAME}/{Config.VERSION}"},
        params={"limit": str(limit)},
    ) as response:
        if response.status != 200:
            return None

        data = (await response.json())["data"]

    filter_nsfw = await RedditSettings.filter_nsfw.get()
    posts: List[dict] = []
    for post in data["children"]:
        # t3 = link
        if post["kind"] == "t3" and post["data"].get("post_hint") == "image":
            if post["data"]["over_18"] and filter_nsfw:
                continue

            posts.append(
                {
                    "id": post["data"]["id"],
                    "author": post["data"]["author"],
                    "title": post["data"]["title"],
                    "created_utc": post["data"]["created_utc"],
                    "score": post["data"]["score"],
                    "num_comments": post["data"]["num_comments"],
                    "permalink": post["data"]["permalink"],
                    "url": post["data"]["url"],
                    "subreddit": post["data"]["subreddit"],
                }
            )
    return posts


def create_embed(post: dict) -> Embed:
    embed = Embed(
        # add a blank character after every : and . to prevent wrong redirects for titles
        title=post["title"].replace(":", ":\u200b").replace(".", ".\u200b"),
        url=f"https://reddit.com{post['permalink']}",
        description=f"{post['score']} :thumbsup: \u00B7 {post['num_comments']} :speech_balloon:",
        colour=Colors.Reddit,  # Reddit's brand color
    )
    embed.set_author(name=f"u/{post['author']}", url=f"https://reddit.com/u/{post['author']}")
    embed.set_image(url=post["url"])
    embed.set_footer(text=f"r/{post['subreddit']}")
    embed.timestamp = datetime.utcfromtimestamp(post["created_utc"])
    return embed


class RedditCog(Cog, name="Reddit"):
    CONTRIBUTORS = [Contributor.Scriptim, Contributor.Defelo, Contributor.wolflu, Contributor.Anorak]

    async def on_ready(self):
        interval = await RedditSettings.interval.get()
        await self.start_loop(interval)

    @tasks.loop()
    @db_wrapper
    async def reddit_loop(self):
        await self.pull_hot_posts()

    async def pull_hot_posts(self):
        logger.info("pulling hot reddit posts")
        limit = await RedditSettings.limit.get()
        async for reddit_channel in await db.stream(select(RedditChannel)):  # type: RedditChannel
            text_channel: Optional[TextChannel | Thread] = self.bot.get_channel(reddit_channel.channel)
            if text_channel is None:
                await db.delete(reddit_channel)
                continue

            try:
                check_message_send_permissions(text_channel, check_embed=True)
            except CommandError:
                await send_alert(self.bot.guilds[0], t.cannot_send(text_channel.mention))
                continue

            posts = await fetch_reddit_posts(reddit_channel.subreddit, limit)
            if posts is None:
                await send_alert(self.bot.guilds[0], t.could_not_fetch(reddit_channel.subreddit))
                continue

            for post in posts:
                if await RedditPost.post(post["id"]):
                    await text_channel.send(embed=create_embed(post))

        await RedditPost.clean()

    async def start_loop(self, interval):
        self.reddit_loop.cancel()
        self.reddit_loop.change_interval(hours=interval)
        try:
            self.reddit_loop.start()
        except Exception as e:
            print(e)
            self.reddit_loop.restart()

    reddit = SlashCommandGroup(
        name="reddit",
        description="Reddit-Integration verwalten",
        guild_only=True,
    )

    @reddit.command(name="overview", description="Übersicht aller Reddit-Verknüpfungen anzeigen")
    @RedditPermission.read.check
    async def reddit_overview(self, ctx: ApplicationContext):
        embed = Embed(title=t.reddit, colour=Colors.Reddit)

        interval = await RedditSettings.interval.get()
        embed.add_field(name=t.interval, value=t.x_hours(cnt=interval))

        limit = await RedditSettings.limit.get()
        embed.add_field(name=t.limit, value=str(limit))

        filter_nsfw = await RedditSettings.filter_nsfw.get()
        embed.add_field(name=t.nsfw_filter, value=tg.enabled if filter_nsfw else tg.disabled, inline=False)

        out = []
        async for reddit_channel in await db.stream(select(RedditChannel)):  # type: RedditChannel
            text_channel: Optional[TextChannel | Thread] = self.bot.get_channel(reddit_channel.channel)
            if text_channel is None:
                await db.delete(reddit_channel)
            else:
                sub = reddit_channel.subreddit
                out.append(f":small_orange_diamond: [r/{sub}](https://reddit.com/r/{sub}) -> {text_channel.mention}")
        embed.add_field(name=t.reddit_links, value="\n".join(out) or t.no_reddit_links, inline=False)

        await reply(ctx, embed=embed)

    @reddit.command(name="add", description="Subreddit mit Kanal verknüpfen")
    @RedditPermission.write.check
    async def reddit_add(
        self,
        ctx: ApplicationContext,
        subreddit: str = Option(str, description="Subreddit-Name"),
        channel: TextChannel = Option(TextChannel, description="Zielkanal"),
    ):
        if not (subreddit_name := await get_subreddit_name(subreddit)):
            raise CommandError(t.subreddit_not_found)

        check_message_send_permissions(channel, check_embed=True)

        if await db.exists(filter_by(RedditChannel, subreddit=subreddit_name, channel=channel.id)):
            raise CommandError(t.reddit_link_already_exists)

        await RedditChannel.create(subreddit_name, channel.id)
        embed = Embed(title=t.reddit, colour=Colors.Reddit, description=t.reddit_link_created)
        await reply(ctx, embed=embed)
        await send_to_changelog(ctx.guild, t.log_reddit_link_created(subreddit_name, channel.mention))

    @reddit.command(name="remove", description="Subreddit-Verknüpfung entfernen")
    @RedditPermission.write.check
    async def reddit_remove(
        self,
        ctx: ApplicationContext,
        subreddit: str = Option(str, description="Subreddit-Name"),
        channel: TextChannel = Option(TextChannel, description="Zielkanal"),
    ):
        subreddit_name = await get_subreddit_name(subreddit) or subreddit
        link: Optional[RedditChannel] = await db.get(RedditChannel, subreddit=subreddit_name, channel=channel.id)
        if link is None:
            raise CommandError(t.reddit_link_not_found)

        await db.delete(link)
        embed = Embed(title=t.reddit, colour=Colors.Reddit, description=t.reddit_link_removed)
        await reply(ctx, embed=embed)
        await send_to_changelog(ctx.guild, t.log_reddit_link_removed(subreddit_name, channel.mention))

    @reddit.command(name="interval", description="Abrufintervall in Stunden ändern")
    @RedditPermission.write.check
    async def reddit_interval(self, ctx: ApplicationContext, hours: int = Option(int, description="Intervall in Stunden")):
        if not 0 < hours < (1 << 31):
            raise CommandError(t.invalid_interval)

        await RedditSettings.interval.set(hours)
        await self.start_loop(hours)
        embed = Embed(title=t.reddit, colour=Colors.Reddit, description=t.reddit_interval_set)
        await reply(ctx, embed=embed)
        await send_to_changelog(ctx.guild, t.log_reddit_interval_set(cnt=hours))

    @reddit.command(name="limit", description="Maximale Anzahl an Beiträgen pro Abruf festlegen")
    @RedditPermission.write.check
    async def reddit_limit(self, ctx: ApplicationContext, limit: int = Option(int, description="Limit")):
        if not 0 < limit < (1 << 31):
            raise CommandError(t.invalid_limit)

        await RedditSettings.limit.set(limit)
        embed = Embed(title=t.reddit, colour=Colors.Reddit, description=t.reddit_limit_set)
        await reply(ctx, embed=embed)
        await send_to_changelog(ctx.guild, t.log_reddit_limit_set(limit))

    @reddit.command(name="nsfw_filter", description="NSFW-Filter aktivieren oder deaktivieren")
    @RedditPermission.write.check
    async def reddit_nsfw_filter(self, ctx: ApplicationContext, enabled: bool = Option(bool, description="Aktivieren")):
        embed = Embed(title=t.reddit, colour=Colors.Reddit)
        await RedditSettings.filter_nsfw.set(enabled)
        if enabled:
            embed.description = t.nsfw_filter_now_enabled
            await send_to_changelog(ctx.guild, t.log_nsfw_filter_now_enabled)
        else:
            embed.description = t.nsfw_filter_now_disabled
            await send_to_changelog(ctx.guild, t.log_nsfw_filter_now_disabled)
        await reply(ctx, embed=embed)

    @reddit.command(name="trigger", description="Reddit-Abruf manuell ausführen")
    @RedditPermission.trigger.check
    async def reddit_trigger(self, ctx: ApplicationContext):
        await self.start_loop(await RedditSettings.interval.get())
        embed = Embed(title=t.reddit, colour=Colors.Reddit, description=t.done)
        await reply(ctx, embed=embed)
