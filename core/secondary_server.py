"""Secondary Plex server support (fork-only feature).

PlexCache-D normally talks to a single Plex server. This module adds an optional
second server that shares the same media folders, cache drive, trackers and
mover exclusion list as the primary one, but has its own owner account and its
own set of users.

Design notes
------------
* The primary server keeps using the existing top-level settings keys
  (``PLEX_URL``, ``PLEX_TOKEN``, ``users``, ``valid_sections`` ...). Nothing about
  an existing install changes until a secondary server is configured, so an
  upgrade never rewrites or loses current data.
* The secondary server lives under a single ``secondary_server`` settings key::

      "secondary_server": {
          "enabled": true,
          "name": "Secondary",
          "plex_url": "https://plex2.example.com",
          "plex_token": "...",
          "users": [ {same shape as the top-level users list} ],
          "admin_plex_id": "12345",       # captured for web UI login
          "admin_username": "owner2"
      }

* Usernames coming from the secondary server are stored in the shared trackers
  with a stable ``@secondary`` suffix (see :func:`qualify_username`), so a
  friend called "alice" on both servers never overwrites the other's data.
* Libraries are selected once (on the primary). The secondary server's
  sections are matched by library title (see :func:`match_sections_by_name`).

Keeping this logic in its own module keeps the diff against upstream small.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

SECONDARY_SETTINGS_KEY = "secondary_server"
PRIMARY_NAME_KEY = "primary_server_name"
DEFAULT_PRIMARY_NAME = "Primary"
DEFAULT_SECONDARY_NAME = "Secondary"

# Stable internal marker appended to secondary-server usernames in trackers.
# Not the display name: renaming the server must not orphan tracker data.
SECONDARY_USER_SUFFIX = "@secondary"

# Backup written the first time a secondary server is saved, so the pre-feature
# settings can always be restored by hand.
PRE_MULTISERVER_BACKUP_NAME = "plexcache_settings.pre-multiserver.json"


# ---------------------------------------------------------------------------
# Username helpers
# ---------------------------------------------------------------------------

def qualify_username(username: str) -> str:
    """Return the tracker key for a secondary-server user."""
    if not username:
        return username
    if username.endswith(SECONDARY_USER_SUFFIX):
        return username
    return f"{username}{SECONDARY_USER_SUFFIX}"


def is_secondary_username(username: Optional[str]) -> bool:
    """True when a tracker username belongs to the secondary server."""
    return bool(username) and username.endswith(SECONDARY_USER_SUFFIX)


def split_username(username: Optional[str]) -> Tuple[str, bool]:
    """Split a tracker username into ``(display_name, is_secondary)``."""
    if is_secondary_username(username):
        return username[: -len(SECONDARY_USER_SUFFIX)], True
    return username or "", False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class SecondaryServerConfig:
    """Settings for the optional secondary Plex server."""

    enabled: bool = False
    name: str = DEFAULT_SECONDARY_NAME
    plex_url: str = ""
    plex_token: str = ""
    users: List[dict] = field(default_factory=list)
    admin_plex_id: str = ""
    admin_username: str = ""
    # Derived from ``users`` (same rules as the primary in core/config.py)
    skip_ondeck: List[str] = field(default_factory=list)
    skip_watchlist: List[str] = field(default_factory=list)
    per_user_ondeck_days: Dict[str, int] = field(default_factory=dict)
    per_user_watchlist_days: Dict[str, float] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        """True when the secondary server should be used during runs."""
        return bool(self.enabled and self.plex_url.strip() and self.plex_token.strip())

    @property
    def home_users(self) -> List[str]:
        return [u.get("title") for u in self.users if u.get("is_local") and u.get("title")]


def load_secondary_config(settings: Dict[str, Any]) -> SecondaryServerConfig:
    """Build a :class:`SecondaryServerConfig` from the raw settings dict."""
    raw = settings.get(SECONDARY_SETTINGS_KEY) or {}
    if not isinstance(raw, dict):
        logging.warning("[SECONDARY] Ignoring malformed 'secondary_server' settings")
        raw = {}

    users = raw.get("users") or []
    if not isinstance(users, list):
        users = []

    cfg = SecondaryServerConfig(
        enabled=bool(raw.get("enabled", False)),
        name=(raw.get("name") or DEFAULT_SECONDARY_NAME).strip() or DEFAULT_SECONDARY_NAME,
        plex_url=str(raw.get("plex_url") or ""),
        plex_token=str(raw.get("plex_token") or ""),
        users=users,
        admin_plex_id=str(raw.get("admin_plex_id") or ""),
        admin_username=str(raw.get("admin_username") or ""),
    )

    for u in users:
        title = u.get("title")
        token = u.get("token")
        if u.get("skip_ondeck"):
            if title:
                cfg.skip_ondeck.append(title)
            if token:
                cfg.skip_ondeck.append(token)
        if u.get("skip_watchlist"):
            if title:
                cfg.skip_watchlist.append(title)
            if token:
                cfg.skip_watchlist.append(token)
        if title:
            if u.get("days_to_monitor") is not None:
                try:
                    cfg.per_user_ondeck_days[title] = int(u["days_to_monitor"])
                except (TypeError, ValueError):
                    pass
            if u.get("watchlist_retention_days") is not None:
                try:
                    cfg.per_user_watchlist_days[title] = float(u["watchlist_retention_days"])
                except (TypeError, ValueError):
                    pass
    return cfg


def get_primary_name(settings: Dict[str, Any]) -> str:
    name = settings.get(PRIMARY_NAME_KEY) or DEFAULT_PRIMARY_NAME
    return str(name).strip() or DEFAULT_PRIMARY_NAME


def qualified_per_user_days(per_user: Dict[str, Any]) -> Dict[str, Any]:
    """Re-key a secondary per-user override dict by tracker username."""
    return {qualify_username(k): v for k, v in (per_user or {}).items()}


# ---------------------------------------------------------------------------
# Library matching
# ---------------------------------------------------------------------------

def _norm(title: str) -> str:
    return " ".join(str(title or "").split()).casefold()


def match_sections_by_name(
    primary_sections: Iterable[Any],
    secondary_sections: Iterable[Any],
    valid_sections: Iterable[int],
) -> Tuple[List[int], List[str]]:
    """Map the primary's selected section ids onto the secondary server.

    Args:
        primary_sections: Library sections of the primary server (objects with
            ``key`` and ``title``, e.g. plexapi ``LibrarySection``).
        secondary_sections: Library sections of the secondary server.
        valid_sections: Section ids selected on the primary.

    Returns:
        ``(secondary_ids, unmatched_titles)``. An empty ``valid_sections`` means
        "all libraries", which maps to an empty list on the secondary as well.
    """
    wanted = {int(s) for s in (valid_sections or [])}
    if not wanted:
        return [], []

    primary_by_id = {}
    for section in primary_sections:
        try:
            primary_by_id[int(section.key)] = section
        except (TypeError, ValueError, AttributeError):
            continue

    secondary_by_title: Dict[str, Any] = {}
    for section in secondary_sections:
        secondary_by_title.setdefault(_norm(getattr(section, "title", "")), section)

    matched: List[int] = []
    unmatched: List[str] = []
    for sid in sorted(wanted):
        p = primary_by_id.get(sid)
        if p is None:
            # Selected id no longer exists on the primary; nothing to match.
            continue
        s = secondary_by_title.get(_norm(p.title))
        if s is None:
            unmatched.append(p.title)
            continue
        matched.append(int(s.key))
        p_locs = sorted(getattr(p, "locations", []) or [])
        s_locs = sorted(getattr(s, "locations", []) or [])
        if p_locs and s_locs and p_locs != s_locs:
            logging.warning(
                f"[SECONDARY] Library '{p.title}' uses different folders on the two servers "
                f"({p_locs} vs {s_locs}). Path mappings are shared, so files from the "
                f"secondary may not be found."
            )
    return matched, unmatched
