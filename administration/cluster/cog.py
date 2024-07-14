from datetime import timedelta

from discord import Embed
from discord.ext import commands
from discord.ext.commands import Context, UserInputError, guild_only, CommandError
from discord.utils import utcnow

from PyDrocsid.cluster_model import ClusterNode
from PyDrocsid.cog import Cog
from PyDrocsid.command import docs
from PyDrocsid.database import db, select
from PyDrocsid.embeds import send_long_embed
from PyDrocsid.emojis import name_to_emoji
from PyDrocsid.environment import CLUSTER_NODE
from PyDrocsid.translations import t
from .colors import Colors
from .permissions import ClusterPermission
from ...contributor import Contributor

tg = t.g
t = t.cluster


class ClusterCog(Cog, name="Cluster"):
    CONTRIBUTORS = [Contributor.TNT2k]

    def __init__(self):
        super().__init__()

    @commands.group()
    @ClusterPermission.read.check
    @guild_only()
    @docs(t.commands.cluster)
    async def cluster(self, ctx: Context):
        if ctx.subcommand_passed is not None:
            if ctx.invoked_subcommand is None:
                raise UserInputError
            return

        embed = Embed(title=t.cluster, colour=Colors.Cluster)
        emoji_map = {
            "active": [":hourglass:", ":ballot_box_with_check:"],
            "transferring": ["", ":twisted_rightwards_arrows:"],
            "disabled": ["", ":no_entry:"],
            "healthy": [":x:", ":white_check_mark:"]
        }
        async for node in await db.stream(select(ClusterNode)):
            value = [
                # first line
                t.info_embed.bot + ": " +
                emoji_map["healthy"][node.timestamp + timedelta(seconds=2) >= utcnow()] +
                emoji_map["active"][node.active] +
                emoji_map["transferring"][node.transferring] +
                emoji_map["disabled"][node.disabled],

                t.info_embed.last_ping + f": <t:{int(node.timestamp.timestamp())}:R>"
            ]
            embed.add_field(name=node.node_name, value="\n".join(value), inline=False)

        embed.add_field(name="** **", value=t.info_embed.explanation, inline=False)

        await send_long_embed(ctx, embed=embed)

    @cluster.command(name="disable")
    @ClusterPermission.disable.check
    @docs(t.commands.disable)
    async def disable_node(self, ctx: Context, name: str, disable: bool):
        # node should exist
        if not (node := await ClusterNode.get(name)):
            raise CommandError(t.node_non_existing)

        # node should not already have the new status
        if node.disabled and disable:
            raise CommandError(t.node_disable.already_disabled)
        elif not node.disabled and not disable:
            raise CommandError(t.node_disable.already_enabled)

        node.disabled = disable
        await ctx.message.add_reaction(name_to_emoji["white_check_mark"])

    @cluster.command(name="transfer")
    @ClusterPermission.transfer.check
    @docs(t.commands.transfer)
    async def transfer(self, ctx: Context):
        if not (own_node := await ClusterNode.get(CLUSTER_NODE)):
            raise CommandError(t.self_not_found)
        if own_node.transferring:
            raise CommandError(t.already_transferring)

        # find a ready node, which is not "self"
        if not (nodes := await ClusterNode.get_all()):
            raise CommandError(t.node_non_existing)
        if not any([node for node in nodes if
                    node.node_name != CLUSTER_NODE and
                    node.transferring == False and
                    node.disabled == False and
                    node.timestamp + timedelta(seconds=2) >= utcnow() and
                    node.active == False]):
            raise CommandError(t.no_ready_node)

        own_node.transferring = True
        await ctx.message.add_reaction(name_to_emoji["white_check_mark"])
