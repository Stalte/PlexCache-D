"""Tests for the fork-only secondary Plex server support.

Sources: core/secondary_server.py, core/secondary_runtime.py and the hooks in
web/services/auth_service.py and web/services/settings_service.py.
"""

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.secondary_server import (
    SECONDARY_USER_SUFFIX,
    is_secondary_username,
    load_secondary_config,
    match_sections_by_name,
    qualified_per_user_days,
    qualify_username,
    split_username,
)
from core.secondary_runtime import (
    SecondaryServerRuntime,
    replay_secondary_ondeck,
    snapshot_secondary_ondeck,
)
from core.plex_api import OnDeckItem
from core.file_operations import OnDeckTracker


# ---------------------------------------------------------------------------
# Username helpers
# ---------------------------------------------------------------------------

class TestUsernames:
    def test_qualify_is_idempotent(self):
        assert qualify_username("alice") == "alice@secondary"
        assert qualify_username("alice@secondary") == "alice@secondary"

    def test_email_like_title_keeps_its_at(self):
        q = qualify_username("bob@gmail.com")
        assert q == "bob@gmail.com@secondary"
        assert split_username(q) == ("bob@gmail.com", True)

    def test_primary_names_untouched(self):
        assert not is_secondary_username("alice")
        assert split_username("alice") == ("alice", False)

    def test_same_friend_on_both_servers_gets_two_keys(self):
        assert "alice" != qualify_username("alice")

    def test_per_user_days_rekeyed(self):
        assert qualified_per_user_days({"alice": 5}) == {"alice@secondary": 5}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_missing_block_is_inactive(self):
        cfg = load_secondary_config({"PLEX_URL": "x"})
        assert not cfg.is_active
        assert cfg.users == []

    def test_disabled_is_inactive(self):
        cfg = load_secondary_config({"secondary_server": {
            "enabled": False, "plex_url": "http://a", "plex_token": "t"}})
        assert not cfg.is_active

    def test_active_and_derived_lists(self):
        cfg = load_secondary_config({"secondary_server": {
            "enabled": True, "name": "Kids", "plex_url": "http://a:32401", "plex_token": "t",
            "users": [
                {"title": "alice", "token": "ta", "skip_ondeck": True, "days_to_monitor": "7"},
                {"title": "bob", "token": None, "skip_watchlist": True,
                 "watchlist_retention_days": 2.5, "is_local": True},
            ]}})
        assert cfg.is_active
        assert cfg.name == "Kids"
        assert cfg.skip_ondeck == ["alice", "ta"]
        assert cfg.skip_watchlist == ["bob"]
        assert cfg.per_user_ondeck_days == {"alice": 7}
        assert cfg.per_user_watchlist_days == {"bob": 2.5}
        assert cfg.home_users == ["bob"]

    def test_malformed_block_ignored(self):
        assert not load_secondary_config({"secondary_server": "nope"}).is_active

    def test_config_manager_exposes_secondary(self, tmp_path):
        from core.config import ConfigManager
        settings = json.loads(open("docs/example_settings.json").read())
        settings["secondary_server"] = {"enabled": True, "plex_url": "http://b", "plex_token": "t2"}
        settings["primary_server_name"] = "Main"
        path = tmp_path / "plexcache_settings.json"
        path.write_text(json.dumps(settings))
        cm = ConfigManager(str(path))
        cm.settings_data = settings
        cm._load_plex_config()
        assert cm.secondary.is_active
        assert cm.primary_server_name == "Main"
        # Primary settings are left exactly as they were
        assert cm.plex.plex_url == settings["PLEX_URL"]


# ---------------------------------------------------------------------------
# Library matching
# ---------------------------------------------------------------------------

def _sec(key, title, locations=None):
    return SimpleNamespace(key=key, title=title, locations=locations or [])


class TestMatchSections:
    def test_matches_by_title_not_id(self):
        primary = [_sec(1, "TV Shows"), _sec(2, "Movies"), _sec(19, "4K Movies")]
        secondary = [_sec(5, "Movies"), _sec(7, "tv  shows")]
        ids, unmatched = match_sections_by_name(primary, secondary, [1, 2])
        assert sorted(ids) == [5, 7]
        assert unmatched == []

    def test_reports_unmatched(self):
        primary = [_sec(1, "TV Shows"), _sec(2, "Movies")]
        secondary = [_sec(5, "Movies")]
        ids, unmatched = match_sections_by_name(primary, secondary, [1, 2])
        assert ids == [5]
        assert unmatched == ["TV Shows"]

    def test_empty_selection_means_all(self):
        assert match_sections_by_name([_sec(1, "A")], [_sec(2, "A")], []) == ([], [])

    def test_location_mismatch_warns(self, caplog):
        primary = [_sec(1, "Movies", ["/movies"])]
        secondary = [_sec(3, "Movies", ["/other"])]
        with caplog.at_level("WARNING"):
            ids, _ = match_sections_by_name(primary, secondary, [1])
        assert ids == [3]
        assert "different folders" in caplog.text


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

def _config_manager(users_toggle=True, valid_sections=(1,)):
    cm = MagicMock()
    cm.plex.users_toggle = users_toggle
    cm.plex.valid_sections = list(valid_sections)
    cm.cache.watchlist_toggle = True
    cm.settings_data = {}
    return cm


def _runtime(manager=None, users=None):
    cfg = load_secondary_config({"secondary_server": {
        "enabled": True, "name": "Second", "plex_url": "http://b", "plex_token": "t",
        "users": users or []}})
    return SecondaryServerRuntime(_config_manager(), manager or MagicMock(), cfg)


class TestRuntime:
    def test_connect_failure_never_raises_and_is_incomplete(self):
        manager = MagicMock()
        manager.connect.side_effect = ConnectionError("refused")
        rt = _runtime(manager)
        assert rt.connect(primary_plex=MagicMock()) is False
        assert not rt.is_complete(watchlist_enabled=False)
        assert rt.get_on_deck_media(10, 5) == []
        assert rt.get_active_session_paths() == []

    def test_no_matching_libraries_marks_incomplete(self):
        manager = MagicMock()
        manager.plex.library.sections.return_value = [_sec(9, "Other")]
        primary = MagicMock()
        primary.library.sections.return_value = [_sec(1, "TV Shows")]
        rt = _runtime(manager)
        assert rt.connect(primary) is False
        assert not rt.is_ondeck_complete()

    def test_ondeck_usernames_are_qualified(self):
        manager = MagicMock()
        manager.plex.library.sections.return_value = [_sec(4, "TV Shows")]
        manager.get_on_deck_media.return_value = [
            OnDeckItem(file_path="/tv/a.mkv", username="alice"),
            OnDeckItem(file_path="/tv/b.mkv", username="owner2", is_current_ondeck=True),
        ]
        manager.get_newly_discovered_users.return_value = []
        manager.is_ondeck_data_complete.return_value = True
        primary = MagicMock()
        primary.library.sections.return_value = [_sec(1, "TV Shows")]
        rt = _runtime(manager)
        assert rt.connect(primary)
        items = rt.get_on_deck_media(10, 5)
        assert [i.username for i in items] == ["alice@secondary", "owner2@secondary"]
        # Uses the secondary's own section ids
        assert manager.get_on_deck_media.call_args[0][0] == [4]
        assert rt.is_ondeck_complete()

    def test_watchlist_usernames_are_qualified(self):
        rt = _runtime()
        rt.connected = True
        rt.manager.get_watchlist_media.return_value = iter([
            ("/movies/x.mkv", "owner2", None, None, "123", "movie"),
        ])
        out = rt.get_watchlist_media(3)
        assert out == [("/movies/x.mkv", "owner2@secondary", None, None, "123", "movie")]

    def test_watchlist_failure_marks_incomplete(self):
        rt = _runtime()
        rt.connected = True
        rt.manager.is_ondeck_data_complete.return_value = True
        rt.manager.is_watchlist_data_complete.return_value = True
        rt.manager.get_watchlist_media.side_effect = RuntimeError("plex.tv down")
        assert rt.get_watchlist_media(3) == []
        assert rt.is_complete(watchlist_enabled=False)
        assert not rt.is_complete(watchlist_enabled=True)

    def test_from_config_none_when_inactive(self):
        cm = _config_manager()
        cm.secondary = load_secondary_config({})
        assert SecondaryServerRuntime.from_config(cm) is None

    def test_new_users_added_to_secondary_block(self):
        rt = _runtime()
        rt._add_new_users_to_settings([{"title": "carol", "skip_ondeck": True}])
        assert rt.config_manager.settings_data["secondary_server"]["users"][0]["title"] == "carol"
        rt.config_manager._save_updated_config.assert_called_once()


# ---------------------------------------------------------------------------
# Outage protection
# ---------------------------------------------------------------------------

class TestOutageReplay:
    def test_snapshot_and_replay_keep_secondary_entries(self, tmp_path):
        tracker = OnDeckTracker(str(tmp_path / "ondeck.json"))
        tracker.update_entry("/m/a.mkv", "alice")                       # primary user
        tracker.update_entry("/m/b.mkv", "bob@secondary",
                             episode_info={"show": "S", "season": 1, "episode": 2},
                             is_current_ondeck=True, rating_key="55")
        tracker.update_entry("/m/c.mkv", "alice")
        tracker.update_entry("/m/c.mkv", "carol@secondary")

        snap = snapshot_secondary_ondeck(tracker._data)
        assert {r["path"] for r in snap} == {"/m/b.mkv", "/m/c.mkv"}
        assert all(r["username"].endswith(SECONDARY_USER_SUFFIX) for r in snap)

        # New run: primary still has a.mkv only; secondary is down.
        tracker.prepare_for_run()
        tracker.update_entry("/m/a.mkv", "alice")
        kept = replay_secondary_ondeck(tracker, snap)
        tracker.cleanup_unseen()

        assert sorted(kept) == ["/m/b.mkv", "/m/c.mkv"]
        assert set(tracker._data) == {"/m/a.mkv", "/m/b.mkv", "/m/c.mkv"}
        assert tracker._data["/m/b.mkv"]["ondeck_users"] == ["bob@secondary"]
        assert tracker._data["/m/b.mkv"]["episode_info"]["episode"] == 2
        assert tracker._data["/m/c.mkv"]["users"] == ["carol@secondary"]

    def test_eviction_skipped_when_secondary_incomplete(self):
        from core.app import PlexCacheApp
        app = PlexCacheApp.__new__(PlexCacheApp)
        app.config_manager = MagicMock()
        app.config_manager.cache.cache_eviction_mode = "smart"
        app.config_manager.cache.watchlist_toggle = False
        app.secondary_runtime = MagicMock()
        app.secondary_runtime.is_complete.return_value = False
        app._get_effective_cache_limit = MagicMock(side_effect=AssertionError("should not get here"))
        assert app._run_smart_eviction() == (0, 0)


# ---------------------------------------------------------------------------
# Web hooks
# ---------------------------------------------------------------------------

sys.modules.setdefault('web.config', MagicMock(
    PROJECT_ROOT=MagicMock(),
    DATA_DIR=MagicMock(),
    SETTINGS_FILE=MagicMock(exists=MagicMock(return_value=False)),
    PLEXCACHE_PRODUCT_VERSION='test',
))


class TestAuthSecondaryOwner:
    @pytest.fixture
    def auth(self, tmp_path):
        settings_file = tmp_path / "plexcache_settings.json"
        with patch('web.services.auth_service.SETTINGS_FILE', settings_file), \
             patch('web.services.auth_service.DATA_DIR', tmp_path), \
             patch('web.services.auth_service.AuthService.SESSIONS_FILE', str(tmp_path / "s.json")):
            from web.services.auth_service import AuthService
            yield AuthService(), settings_file

    def _login(self, service, account_id):
        account = MagicMock(id=account_id, username="who")
        myplex = MagicMock()
        myplex.MyPlexAccount.return_value = account
        with patch.dict('sys.modules', {'plexapi': MagicMock(), 'plexapi.myplex': myplex}):
            return service.validate_plex_login("tok")

    def test_secondary_owner_can_log_in(self, auth):
        service, settings_file = auth
        settings_file.write_text(json.dumps({
            "auth_admin_plex_id": "1",
            "secondary_server": {"enabled": True, "admin_plex_id": "2"}}))
        assert self._login(service, 1) is not None
        assert self._login(service, 2) is not None
        assert self._login(service, 3) is None

    def test_disabled_secondary_owner_rejected(self, auth):
        service, settings_file = auth
        settings_file.write_text(json.dumps({
            "auth_admin_plex_id": "1",
            "secondary_server": {"enabled": False, "admin_plex_id": "2"}}))
        assert self._login(service, 2) is None


class TestExportRedaction:
    def test_secondary_secrets_redacted(self, tmp_path):
        from web.services.settings_service import SettingsService
        svc = SettingsService.__new__(SettingsService)
        raw = {"PLEX_URL": "http://a:32400", "PLEX_TOKEN": "p", "users": [],
               "secondary_server": {"enabled": True, "plex_url": "https://plex2.example.com",
                                    "plex_token": "secret", "admin_plex_id": "2",
                                    "users": [{"title": "alice", "token": "tt", "id": 9, "uuid": "u"}]}}
        svc._load_raw = lambda: json.loads(json.dumps(raw))
        svc.settings_file = tmp_path / "plexcache_settings.json"
        out = svc.export_settings(include_sensitive=False)
        sec = out["secondary_server"]
        assert sec["plex_token"] == ""
        assert sec["plex_url"] == "[REDACTED]"
        assert sec["users"][0]["token"] == ""
        assert sec["users"][0]["title"] == "Secondary User 1"
