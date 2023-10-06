from typing import Optional, Union

from sqlalchemy import BigInteger, Column

from PyDrocsid.database import Base, db


class NewsAuthorization(Base):
    __tablename__ = "news_authorization"

    key: Union[Column, int] = Column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Union[Column, int] = Column(BigInteger)
    channel_id: Union[Column, int] = Column(BigInteger)
    notification_role_id: Union[Column, int] = Column(BigInteger, nullable=True)

    @staticmethod
    async def create(source_id: int, channel_id: int, notification_role_id: Optional[int]) -> "NewsAuthorization":
        row = NewsAuthorization(source_id=source_id, channel_id=channel_id, notification_role_id=notification_role_id)
        await db.add(row)
        return row
