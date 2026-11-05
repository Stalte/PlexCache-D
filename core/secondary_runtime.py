"""Run-time handling of the optional secondary Plex server (fork-only feature).

``PlexCacheApp`` owns one :class:`SecondaryServerRuntime` when a secondary
server is configured. The runtime wraps a second :class:`PlexManager` and
returns OnDeck / watchlist results with server-qualified usernames, ready to be
merged into the primary's results. Everything downstream (trackers, cache
moves, eviction, the exclusion list) stays shared, which is what keeps the two
servers from fighting over the cache.

If the secondary server cannot be reached, or returns partial data, the runtime
reports itself as *incomplete*. The app then keeps every file the secondary's
users were protecting (replayed from the OnDeck tracker), and skips move-back
and eviction for that run, the same way it already reacts to a plex.tv outage.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict, List, Optional, Tuple

from core.plex_api import OnDeckItem, PlexManager
from core.secondary_server import (
    SECONDARY_SETTINGS_KEY,
    SecondaryServerConfig,
    is_secondary_username,
    match_sections_by_name,
    qualified_per_user_days,
    qualify_username,
)

LOG_PREFIX = "[SECONDARY]"


class SecondaryServerRuntime:
    """Second Plex connection used alongside the primary during a run."""

    def __init__(self, config_manager: Any, manager: PlexManager, cfg: SecondaryServerConfig):
        self.config_manager = config_manager
        self.manager = manager
        self.cfg = cfg
        self.connected = False
        self.valid_sections: List[int] = []
        self.unmatched_libraries: List[str] = []
        self._failed_reason: Optional[str] = None
        self._watchlist_failed = False

    # ------------------------------------------------------------------
    # Construction / connection
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config_manager: Any) -> Optional["SecondaryServerRuntime"]:
        cfg: SecondaryServerConfig = getattr(config_manager, "secondary", None)
        if cfg is None or not cfg.is_active:
            return None
        manager = PlexManager(
            plex_url=cfg.plex_url,
            plex_token=cfg.plex_token,
            retry_limit=config_manager.performance.retry_limit,
            delay=config_manager.performance.delay,
            token_cache_file=str(config_manager.get_secondary_user_tokens_file()),
            rss_cache_file=str(config_manager.get_secondary_rss_cache_file()),
            plex_db_path="",
            watchlist_enabled=config_manager.cache.watchlist_toggle,
        )
        return cls(config_manager, manager, cfg)

    @property
    def name(self) -> str:
        return self.cfg.name

    def connect(self, primary_plex: Any) -> bool:
        """Connect, load user tokens and match libraries. Never raises."""
        try:
            self.manager.connect()
        except Exception as e:  # ConnectionError from PlexManager, or anything unexpected
            self._failed_reason = str(e)
            logging.warning(
                f"{LOG_PREFIX} '{self.name}' is unreachable ({e}). Continuing with the "
                f"primary server only; files the '{self.name}' users were keeping on "
                f"cache stay protected this run."
            )
            return False

        self.connected = True
        try:
            self._match_libraries(primary_plex)
        except Exception as e:
            self._failed_reason = f"library matching failed: {e}"
            logging.error(f"{LOG_PREFIX} Could not match libraries on '{self.name}': {e}")
            self.connected = False
            return False

        plex_cfg = self.config_manager.plex
        if plex_cfg.users_toggle:
            skip_users = list(set(self.cfg.skip_ondeck) & set(self.cfg.skip_watchlist))
            try:
                self.manager.load_user_tokens(
                    skip_users=skip_users,
                    settings_users=self.cfg.users,
                    main_username="main",
                )
                new_users = self.manager.get_newly_discovered_users()
                if new_users:
                    self._add_new_users_to_settings(new_users)
            except Exception as e:
                logging.error(f"{LOG_PREFIX} Failed to load users for '{self.name}': {e}")
                # Without user tokens, friends' OnDeck would look empty.
                self._failed_reason = f"user loading failed: {e}"
        logging.info(f"{LOG_PREFIX} Connected to '{self.name}' ({len(self.valid_sections) or 'all'} libraries)")
        return True

    def _match_libraries(self, primary_plex: Any) -> None:
        valid = self.config_manager.plex.valid_sections or []
        if not valid:
            self.valid_sections = []
            return
        primary_sections = primary_plex.library.sections() if primary_plex is not None else []
        secondary_sections = self.manager.plex.library.sections()
        self.valid_sections, self.unmatched_libraries = match_sections_by_name(
            primary_sections, secondary_sections, valid
        )
        if self.unmatched_libraries:
            logging.warning(
                f"{LOG_PREFIX} These libraries were not found by name on '{self.name}' and are "
                f"skipped there: {', '.join(self.unmatched_libraries)}"
            )
        if not self.valid_sections:
            # Never fall back to "all libraries" when the user picked specific ones.
            raise RuntimeError("none of the selected libraries exist on this server")

    def _add_new_users_to_settings(self, new_users: List[dict]) -> None:
        """Auto-add newly discovered secondary users (tracking disabled)."""
        settings = self.config_manager.settings_data
        section = settings.setdefault(SECONDARY_SETTINGS_KEY, {})
        users = section.setdefault("users", [])
        existing = {u.get("title") for u in users}
        added = 0
        for info in new_users:
            title = info.get("title")
            if not title or title in existing:
                continue
            users.append(info)
            existing.add(title)
            added += 1
            logging.warning(
                f"{LOG_PREFIX} Auto-added new '{self.name}' user '{title}' with tracking "
                f"disabled. Enable it under Settings > Secondary Server."
            )
        if added:
            self.cfg.users = users
            try:
                self.config_manager._save_updated_config()
            except OSError as e:
                logging.error(f"{LOG_PREFIX} Failed to save new users: {e}")

    # ------------------------------------------------------------------
    # Completeness
    # ------------------------------------------------------------------

    def is_ondeck_complete(self) -> bool:
        return (self.connected and self._failed_reason is None
                and self.manager.is_ondeck_data_complete())

    def is_watchlist_complete(self) -> bool:
        return (self.connected and self._failed_reason is None and not self._watchlist_failed
                and self.manager.is_watchlist_data_complete())

    def is_complete(self, watchlist_enabled: bool) -> bool:
        if not self.is_ondeck_complete():
            return False
        if watchlist_enabled and not self.is_watchlist_complete():
            return False
        return True

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def get_active_session_paths(self) -> List[str]:
        """Plex-form file paths currently being streamed from this server."""
        if not self.connected:
            return []
        paths: List[str] = []
        try:
            sessions = self.manager.get_active_sessions()
        except Exception as e:
            logging.warning(f"{LOG_PREFIX} Could not read active sessions on '{self.name}': {e}")
            return []
        for session in sessions or []:
            try:
                for media in getattr(session, "media", []) or []:
                    for part in getattr(media, "parts", []) or []:
                        if getattr(part, "file", None):
                            paths.append(part.file)
                            break
                    break
            except (AttributeError, TypeError):
                continue
        return paths

    def get_on_deck_media(self, days_to_monitor: int, number_episodes: int,
                          prefetch_minimum_minutes: int = 0) -> List[OnDeckItem]:
        """OnDeck items from the secondary server with qualified usernames."""
        if not self.connected:
            return []
        plex_cfg = self.config_manager.plex
        try:
            items = self.manager.get_on_deck_media(
                self.valid_sections,
                days_to_monitor,
                number_episodes,
                plex_cfg.users_toggle,
                self.cfg.skip_ondeck,
                per_user_days=self.cfg.per_user_ondeck_days or None,
                prefetch_minimum_minutes=prefetch_minimum_minutes,
            )
        except Exception as e:
            logging.error(f"{LOG_PREFIX} OnDeck fetch failed on '{self.name}': {e}")
            self._failed_reason = f"OnDeck fetch failed: {e}"
            return []
        result = [dataclasses.replace(i, username=qualify_username(i.username)) for i in items]
        users = {i.username for i in result}
        logging.info(f"[FETCH] OnDeck ({self.name}): {len(result)} items from {len(users)} users")
        return result

    def get_watchlist_media(self, watchlist_episodes: int) -> List[Tuple]:
        """Watchlist tuples from the secondary server with qualified usernames."""
        if not self.connected:
            return []
        plex_cfg = self.config_manager.plex
        try:
            items = list(self.manager.get_watchlist_media(
                self.valid_sections,
                watchlist_episodes,
                plex_cfg.users_toggle,
                self.cfg.skip_watchlist,
                home_users=self.cfg.home_users,
            ))
        except Exception as e:
            logging.error(f"{LOG_PREFIX} Watchlist fetch failed on '{self.name}': {e}")
            self._watchlist_failed = True
            return []
        out = []
        for item in items:
            item = list(item)
            item[1] = qualify_username(item[1])
            out.append(tuple(item))
        logging.info(f"[FETCH] Watchlist ({self.name}): {len(out)} items")
        return out

    # ------------------------------------------------------------------
    # Per-user overrides keyed by tracker username
    # ------------------------------------------------------------------

    def qualified_ondeck_days(self) -> Dict[str, int]:
        return qualified_per_user_days(self.cfg.per_user_ondeck_days)

    def qualified_watchlist_days(self) -> Dict[str, float]:
        return qualified_per_user_days(self.cfg.per_user_watchlist_days)


# ----------------------------------------------------------------------
# Outage protection helpers (pure functions, unit-tested)
# ----------------------------------------------------------------------

def snapshot_secondary_ondeck(tracker_data: Dict[str, dict]) -> List[Dict[str, Any]]:
    """Capture the secondary users' OnDeck entries before a run resets them.

    Returns a list of ``{path, username, episode_info, is_current, rating_key}``.
    """
    snap: List[Dict[str, Any]] = []
    for path, entry in (tracker_data or {}).items():
        current = set(entry.get("ondeck_users", []) or [])
        ep = entry.get("episode_info")
        for user in entry.get("users", []) or []:
            if is_secondary_username(user):
                snap.append({
                    "path": path,
                    "username": user,
                    "episode_info": dict(ep) if isinstance(ep, dict) else None,
                    "is_current": user in current,
                    "rating_key": entry.get("rating_key"),
                })
    return snap


def replay_secondary_ondeck(tracker: Any, snapshot: List[Dict[str, Any]]) -> List[str]:
    """Re-register last run's secondary OnDeck entries; return their paths."""
    paths: List[str] = []
    seen = set()
    for row in snapshot:
        tracker.update_entry(
            row["path"],
            row["username"],
            episode_info=row.get("episode_info"),
            is_current_ondeck=row.get("is_current", False),
            rating_key=row.get("rating_key"),
        )
        if row["path"] not in seen:
            seen.add(row["path"])
            paths.append(row["path"])
    return paths
