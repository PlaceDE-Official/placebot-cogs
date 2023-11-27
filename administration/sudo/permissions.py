from enum import auto

from PyDrocsid.permission import BasePermission
from PyDrocsid.translations import t


class SudoPermission(BasePermission):
    @property
    def description(self) -> str:
        return t.sudo.permissions[self.name]

    clear_cache = auto()
    reload = auto()
    restart = auto()
    toggle_maintenance = auto()
    bypass_maintenance = auto()
    show_sudoers = auto()
    stop = auto()
    kill = auto()
