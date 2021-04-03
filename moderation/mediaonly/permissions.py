from enum import auto

from PyDrocsid.permission import BasePermission
from PyDrocsid.translations import t


class MediaOnlyPermission(BasePermission):
    @property
    def description(self) -> str:
        return t.mediaonly.permissions[self.name]

    bypass = auto()
    manage = auto()
