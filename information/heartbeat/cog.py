from datetime import datetime
from pathlib import Path
from typing import Optional

from discord import Forbidden, User
from discord.ext import tasks
from discord.utils import format_dt, utcnow

from PyDrocsid.cog import Cog
from PyDrocsid.config import Config
from PyDrocsid.environment import CLUSTER_NODE
from PyDrocsid.translations import t
from PyDrocsid.util import get_owners, send_editable_log, write_healthcheck

from ...contributor import Contributor


tg = t.g
t = t.heartbeat


class HeartbeatCog(Cog, name="Heartbeat"):
    CONTRIBUTORS = [Contributor.Defelo, Contributor.wolflu, Contributor.TNT2k]

    def __init__(self):
        super().__init__()

        self.initialized = False

    @tasks.loop(seconds=20)
    async def status_loop(self):
        try:
            now = utcnow()
            write_healthcheck()
            for owner in get_owners(self.bot):
                try:
                    await send_editable_log(
                        owner,
                        t.online_status_cluster(CLUSTER_NODE) if CLUSTER_NODE else t.online_status,
                        t.status_description(Config.NAME, Config.VERSION),
                        [(t.heartbeat, format_dt(now, style="D") + " " + format_dt(now, style="T"))],
                    )

                except Forbidden:
                    pass
        except Exception as e:
            print(e)

    async def on_ready(self):
        owners = get_owners(self.bot)
        now = utcnow()
        for owner in owners:
            try:
                await send_editable_log(
                    owner,
                    t.online_status_cluster(CLUSTER_NODE) if CLUSTER_NODE else t.online_status,
                    t.status_description(Config.NAME, Config.VERSION),
                    [(t.logged_in, format_dt(now, style="D") + " " + format_dt(now, style="T"))],
                    force_resend=True,
                    force_new_embed=not self.initialized,
                )
            except Forbidden:
                pass

        try:
            self.status_loop.start()
        except Exception as e:
            print(e)
            self.status_loop.restart()

        self.initialized = True
