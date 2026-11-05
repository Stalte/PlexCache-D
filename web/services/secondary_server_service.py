"""Settings for the optional secondary Plex server (fork-only feature).

Reads and writes the ``secondary_server`` block of plexcache_settings.json
through the shared SettingsService so writes stay atomic. The first save also
leaves a copy of the settings file as it was before multi-server support, so
the original single-server setup can always be restored by hand.
"""

import logging
import shutil
from typing import Any, Dict, List, Optional, Tuple

from core.secondary_server import (
    DEFAULT_PRIMARY_NAME,
    DEFAULT_SECONDARY_NAME,
    PRE_MULTISERVER_BACKUP_NAME,
    PRIMARY_NAME_KEY,
    SECONDARY_SETTINGS_KEY,
)
from web.services import get_settings_service

logger = logging.getLogger(__name__)


def _service():
    return get_settings_service()


def get_secondary_settings() -> Dict[str, Any]:
    raw = _service()._load_raw()
    sec = raw.get(SECONDARY_SETTINGS_KEY) or {}
    return {
        "enabled": bool(sec.get("enabled", False)),
        "name": sec.get("name") or DEFAULT_SECONDARY_NAME,
        "plex_url": sec.get("plex_url", ""),
        "plex_token": sec.get("plex_token", ""),
        "users": sec.get("users", []) or [],
        "admin_username": sec.get("admin_username", ""),
        "primary_name": raw.get(PRIMARY_NAME_KEY) or DEFAULT_PRIMARY_NAME,
        "users_toggle": raw.get("users_toggle", True),
        "days_to_monitor": raw.get("days_to_monitor", 183),
        "watchlist_retention_days": raw.get("watchlist_retention_days", 0),
    }


def write_pre_multiserver_backup() -> Optional[str]:
    """Copy the settings file once, before the first secondary-server save."""
    svc = _service()
    src = svc.settings_file
    dest = src.parent / PRE_MULTISERVER_BACKUP_NAME
    if dest.exists() or not src.exists():
        return None
    try:
        shutil.copy2(src, dest)
        try:
            dest.chmod(0o600)
        except OSError:
            pass
        logger.info("Saved pre-multi-server settings backup to %s", dest)
        return str(dest)
    except OSError as e:
        logger.error("Could not write pre-multi-server settings backup: %s", e)
        return None


def _connect(plex_url: str, plex_token: str, timeout: int = 15):
    from plexapi.server import PlexServer
    return PlexServer(plex_url, plex_token, timeout=timeout)


def _friendly_error(e: Exception) -> str:
    msg = str(e)
    if "Connection refused" in msg or "Errno 111" in msg:
        return "Cannot connect to the Plex server. Is it running?"
    if "timed out" in msg.lower() or "timeout" in msg.lower():
        return "Connection timed out. Check the URL."
    if "401" in msg or "Unauthorized" in msg:
        return "Invalid Plex token. Try signing in again."
    return f"Plex error: {msg[:120]}"


def test_connection(plex_url: str, plex_token: str) -> Tuple[bool, str]:
    if not plex_url or not plex_token:
        return False, "Enter a URL and a token first"
    try:
        plex = _connect(plex_url, plex_token, timeout=10)
        return True, f"Connected to {plex.friendlyName} (v{plex.version})"
    except Exception as e:
        return False, _friendly_error(e)


def save_connection(enabled: bool, name: str, plex_url: str, plex_token: str,
                    primary_name: str) -> Tuple[bool, str]:
    """Save URL/token/names. Captures the owner identity for web login."""
    svc = _service()
    write_pre_multiserver_backup()

    raw = svc._load_raw()
    sec = dict(raw.get(SECONDARY_SETTINGS_KEY) or {})
    plex_url = (plex_url or "").strip()
    plex_token = (plex_token or "").strip()
    token_changed = plex_token != sec.get("plex_token", "")

    sec["enabled"] = bool(enabled)
    sec["name"] = (name or "").strip() or DEFAULT_SECONDARY_NAME
    sec["plex_url"] = plex_url
    sec["plex_token"] = plex_token
    sec.setdefault("users", [])
    raw[PRIMARY_NAME_KEY] = (primary_name or "").strip() or DEFAULT_PRIMARY_NAME

    note = ""
    if plex_token and (token_changed or not sec.get("admin_plex_id")):
        try:
            from plexapi.myplex import MyPlexAccount
            account = MyPlexAccount(token=plex_token)
            sec["admin_plex_id"] = str(getattr(account, "id", "") or "")
            sec["admin_username"] = getattr(account, "username", "") or getattr(account, "title", "")
        except Exception as e:
            logger.warning("Could not read the secondary owner's Plex account: %s", e)
            note = " (couldn't confirm the owner account, so web login for it may not work yet)"

    raw[SECONDARY_SETTINGS_KEY] = sec
    if not svc._save_raw(raw):
        return False, "Failed to save settings"
    return True, "Secondary server saved" + note


def sync_users() -> Dict[str, Any]:
    """Pull the secondary owner's users, keeping each user's preferences."""
    svc = _service()
    raw = svc._load_raw()
    sec = dict(raw.get(SECONDARY_SETTINGS_KEY) or {})
    plex_url, plex_token = sec.get("plex_url", ""), sec.get("plex_token", "")
    if not plex_url or not plex_token:
        return {"success": False, "error": "Save the secondary server's URL and token first."}
    try:
        plex = _connect(plex_url, plex_token)
        account = plex.myPlexAccount()
        machine_id = plex.machineIdentifier
    except Exception as e:
        return {"success": False, "error": _friendly_error(e)}

    existing = {u.get("title"): u for u in sec.get("users", []) or []}
    # A brand-new user starts with tracking ON, same as a first sync on the primary;
    # users auto-discovered during a run are added with tracking OFF instead.
    new_users: List[Dict[str, Any]] = []

    def keep_prefs(entry: Dict[str, Any], old: Dict[str, Any]) -> Dict[str, Any]:
        entry["skip_ondeck"] = old.get("skip_ondeck", False)
        entry["skip_watchlist"] = old.get("skip_watchlist", False)
        for k in ("days_to_monitor", "watchlist_retention_days"):
            if k in old:
                entry[k] = old[k]
        return entry

    admin_name = account.title or account.username
    new_users.append(keep_prefs({
        "title": admin_name,
        "id": getattr(account, "id", None),
        "uuid": getattr(account, "uuid", None),
        "token": plex_token,
        "is_local": True,
        "is_admin": True,
    }, existing.get(admin_name, {})))

    try:
        shared = account.users()
    except Exception as e:
        return {"success": False, "error": f"Could not list users: {_friendly_error(e)}"}

    for user in shared:
        name = user.title
        old = existing.get(name, {})
        try:
            token = user.get_token(machine_id)
        except Exception:
            token = None
        if token is None:
            token = old.get("token")
        user_uuid = None
        thumb = getattr(user, "thumb", "") or ""
        if "/users/" in thumb:
            try:
                user_uuid = thumb.split("/users/")[1].split("/")[0]
            except (IndexError, AttributeError):
                pass
        new_users.append(keep_prefs({
            "title": name,
            "id": getattr(user, "id", None),
            "uuid": user_uuid,
            "token": token,
            "is_local": bool(getattr(user, "home", False)),
            "is_admin": False,
        }, old))

    added = len({u["title"] for u in new_users} - set(existing))
    removed = len(set(existing) - {u["title"] for u in new_users})
    sec["users"] = new_users
    if not sec.get("admin_plex_id"):
        sec["admin_plex_id"] = str(getattr(account, "id", "") or "")
        sec["admin_username"] = account.username or account.title or ""
    raw[SECONDARY_SETTINGS_KEY] = sec
    if not svc._save_raw(raw):
        return {"success": False, "error": "Failed to save settings"}
    return {"success": True, "users": new_users, "added_count": added, "removed_count": removed}


def save_user_preferences(form: Dict[str, str]) -> bool:
    """Apply the users table form (checkbox/number fields keyed by row index)."""
    svc = _service()
    raw = svc._load_raw()
    sec = dict(raw.get(SECONDARY_SETTINGS_KEY) or {})
    users = sec.get("users", []) or []
    for i, user in enumerate(users):
        if user.get("is_admin"):
            # The owner is always fetched as the server's main account.
            user["skip_ondeck"] = False
            user["skip_watchlist"] = False
        else:
            user["skip_ondeck"] = form.get(f"include_ondeck_{i}") != "on"
            user["skip_watchlist"] = form.get(f"include_watchlist_{i}") != "on"
        token = (form.get(f"user_token_{i}") or "").strip()
        if token:
            user["token"] = token
        for field, cast in (("days_to_monitor", int), ("watchlist_retention_days", float)):
            val = (form.get(f"{field}_{i}") or "").strip()
            if not val:
                user.pop(field, None)
                continue
            try:
                user[field] = cast(val)
            except ValueError:
                user.pop(field, None)
                logger.warning("Ignoring invalid %s '%s' for secondary user '%s'",
                               field, val, user.get("title"))
    sec["users"] = users
    raw[SECONDARY_SETTINGS_KEY] = sec
    return svc._save_raw(raw)
