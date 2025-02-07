from enum import auto

from PyDrocsid.permission import BasePermission
from PyDrocsid.translations import t


class NewsPermission(BasePermission):
    @property
    def description(self) -> str:
        return t.news.permissions[self.name]

    read = auto()
    write = auto()
    view_own = auto()
    view_other = auto()
