from enum import auto

from PyDrocsid.permission import BasePermission
from PyDrocsid.translations import t


class ClusterPermission(BasePermission):
    @property
    def description(self) -> str:
        return t.cluster.permissions[self.name]

    read = auto()
    transfer = auto()
    disable = auto()
