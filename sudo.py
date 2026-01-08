from __future__ import annotations
import config

def is_owner(user_id: int | None) -> bool:
    """Checks if the user is the primary bot owner."""
    if user_id is None:
        return False
    return user_id == config.OWNER_ID

def is_sudo(user_id: int | None) -> bool:
    """
    Checks if the user is either the owner or in the sudo list.
    Note: config.SUDO_USERS should be updated by persistence.py at runtime.
    """
    if user_id is None:
        return False
    # Owners are implicitly sudo users
    if user_id == config.OWNER_ID:
        return True
    return user_id in config.SUDO_USERS
