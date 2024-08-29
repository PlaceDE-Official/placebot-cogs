from enum import auto

from PyDrocsid.permission import BasePermission
from PyDrocsid.translations import t


class VoiceChannelPermission(BasePermission):
    @property
    def description(self) -> str:
        return t.voice_channel.permissions[self.name]

    override_owner = auto()
    dyn_read = auto()
    dyn_write = auto()
    dyn_rename = auto()
    link_read = auto()
    link_write = auto()
    log_read = auto()
    log_set = auto()
    dyn_whitelist_write = auto()
    dyn_whitelist_read = auto()
    dyn_whitelist_check = auto()
    dyn_whitelist_check_parts = auto()
    dyn_whitelist_list = auto()
    vcstatus_logchannel = auto()
