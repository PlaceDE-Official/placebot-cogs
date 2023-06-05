from PyDrocsid.settings import Settings


class LoggingSettings(Settings):
    maxage = -1
    edit_mindiff = 1

    edit_channel = -1
    delete_channel = -1
    alert_channel = -1
    changelog_channel = -1
    member_join_channel = -1
    member_leave_channel = -1
    member_name_change_channel = -1
    # member_profile_picture_change_channel = -1
