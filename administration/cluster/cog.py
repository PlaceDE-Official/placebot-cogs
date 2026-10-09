from datetime import timedelta

from discord import Embed, Option, SlashCommandGroup
from discord.commands import ApplicationContext
from discord.ext.commands import CommandError
from discord.utils import utcnow

from PyDrocsid.cluster_model import ClusterNode
from PyDrocsid.cog import Cog
from PyDrocsid.command import reply
from PyDrocsid.database import db, select
from PyDrocsid.embeds import send_long_embed
from PyDrocsid.environment import CLUSTER_NODE, CLUSTER_HEARTBEAT_TIMEOUT
from PyDrocsid.translations import t
from cluster import sort_nodes
from .colors import Colors
from .permissions import ClusterPermission
from ...contributor import Contributor

tg = t.g
t = t.cluster


class ClusterCog(Cog, name="Cluster"):
    CONTRIBUTORS = [Contributor.TNT2k]

    def __init__(self):
        super().__init__()

    cluster = SlashCommandGroup(
        name="cluster",
        description="Cluster-Status und -Knoten verwalten",
        guild_only=True,
    )

    @cluster.command(name="status", description="Cluster-Status anzeigen")
    @ClusterPermission.read.check
    async def cluster_status(self, ctx: ApplicationContext):
        embed = Embed(title=t.cluster, colour=Colors.Cluster)
        emoji_map = {
            "active": [":hourglass:", ":ballot_box_with_check:"],
            "transferring": ["", ":twisted_rightwards_arrows:"],
            "disabled": ["", ":no_entry:"],
            "healthy": [":x:", ":white_check_mark:"]
        }
        for node in await sort_nodes(await db.all(select(ClusterNode))):
            healthy = node.timestamp + timedelta(seconds=CLUSTER_HEARTBEAT_TIMEOUT) >= utcnow()
            value = [
                # first line
                t.info_embed.bot + ": " +
                emoji_map["healthy"][healthy] +
                ["", emoji_map["active"][node.active]][healthy] +
                emoji_map["transferring"][node.transferring] +
                emoji_map["disabled"][node.disabled],

                t.info_embed.last_ping + f": <t:{int(node.timestamp.timestamp())}:R>"
            ]
            embed.add_field(name=node.node_name, value="\n".join(filter(lambda x: x, value)), inline=False)

        embed.add_field(name="** **", value=t.info_embed.explanation, inline=False)

        await send_long_embed(ctx, embed=embed)

    @cluster.command(name="disable", description="Einen Cluster-Knoten deaktivieren oder aktivieren")
    @ClusterPermission.disable.check
    async def disable_node(
        self,
        ctx: ApplicationContext,
        name: str = Option(str, description="Name des Knotens"),
        disable: bool = Option(bool, description="Deaktivieren (True) oder Aktivieren (False)"),
    ):
        # node should exist
        if not (node := await ClusterNode.get(name)):
            raise CommandError(t.node_non_existing)

        # node should not already have the new status
        if node.disabled and disable:
            raise CommandError(t.node_disable.already_disabled)
        elif not node.disabled and not disable:
            raise CommandError(t.node_disable.already_enabled)

        node.disabled = disable
        await reply(ctx, content=":white_check_mark: Status des Knotens aktualisiert.")

    @cluster.command(name="transfer", description="Cluster-Transferierung anfordern")
    @ClusterPermission.transfer.check
    async def transfer(self, ctx: ApplicationContext):
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
        await reply(ctx, content=":white_check_mark: Transferierung gestartet.")
