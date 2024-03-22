from __future__ import annotations

from datetime import datetime
from typing import AsyncIterable, Union

from discord.utils import utcnow
from sqlalchemy import BigInteger, Boolean, Column, Integer, Text

from PyDrocsid.database import Base, UTCDateTime, db, delete, filter_by, select
from PyDrocsid.environment import CACHE_TTL
from PyDrocsid.redis_client import redis


class MediaOnlyChannel(Base):
    __tablename__ = "mediaonly_channel"

    channel: Union[Column, int] = Column(BigInteger, primary_key=True, unique=True)
    mode: Union[Column, int] = Column(BigInteger, default=1, nullable=False)
    max_length: Union[Column, int] = Column(BigInteger, default=0)
    log: Union[Column, bool] = Column(Boolean, default=False, nullable=False)

    @staticmethod
    async def add(channel: int, mode: int, max_length: int, log: bool):
        await redis.setex(f"mediaonly:channel={channel}", CACHE_TTL, f"1;{mode};{max_length};{int(log)}")
        await db.add(MediaOnlyChannel(channel=channel, mode=mode, max_length=max_length, log=log))

    @staticmethod
    async def get(channel: int) -> tuple[bool, int, int, bool]:
        if result := await redis.get(key := f"mediaonly:channel={channel}"):
            split = result.split(";")
            return split[0] == "1", int(split[1]), int(split[2]), bool(int(split[3]))

        if result := await db.first(filter_by(MediaOnlyChannel, channel=channel)):
            await redis.setex(key, CACHE_TTL, f"1;{result.mode};{result.max_length};{int(result.log)}")
            return True, int(result.mode), int(result.max_length), bool(result.log)
        await redis.setex(f"mediaonly:channel={channel}", CACHE_TTL, "0;0;0;0")
        return False, 0, 0, False

    @staticmethod
    async def stream() -> AsyncIterable[MediaOnlyChannel]:
        row: MediaOnlyChannel
        async with redis.pipeline() as pipe:
            async for row in await db.stream(select(MediaOnlyChannel)):
                await pipe.setex(
                    f"mediaonly:channel={row.channel}", CACHE_TTL, f"1;{row.mode};{row.max_length};{int(row.log)}"
                )
                yield row
            await pipe.execute()

    @staticmethod
    async def remove(channel: int):
        await redis.delete(f"mediaonly:channel={channel}")
        await db.exec(delete(MediaOnlyChannel).filter_by(channel=channel))


class MediaOnlyDeletion(Base):
    __tablename__ = "mediaonly_deletion"

    id: Union[Column, int] = Column(Integer, primary_key=True, unique=True, autoincrement=True)
    member: Union[Column, int] = Column(BigInteger)
    member_name: Union[Column, str] = Column(Text)
    channel: Union[Column, int] = Column(BigInteger)
    timestamp: Union[Column, datetime] = Column(UTCDateTime)

    @staticmethod
    async def create(member: int, member_name: str, channel: int) -> MediaOnlyDeletion:
        row = MediaOnlyDeletion(member=member, member_name=member_name, timestamp=utcnow(), channel=channel)
        await db.add(row)
        return row
