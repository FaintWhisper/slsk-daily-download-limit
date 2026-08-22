import importlib.util
import json
import sys
import types
import unittest

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from tempfile import TemporaryDirectory


class FakeBasePlugin:
    path = None
    core = None
    human_name = "Daily Download Limit"

    def __init__(self, *args, **kwargs):
        self.logs = []
        self.private_messages = []

    def log(self, message, args=None):
        self.logs.append((message, args))

    def send_private(self, username, message, show_ui=True, switch_page=True):
        self.private_messages.append(
            (username, message, show_ui, switch_page)
        )


class FakeNetworkFilter:
    def __init__(self):
        self.banned_users = set()
        self.banned_ips = {}
        self.user_ban_calls = []
        self.ip_ban_calls = []
        self.user_unban_calls = []
        self.ip_unban_calls = []

    def is_user_banned(self, username):
        return username in self.banned_users

    def ban_user(self, username):
        self.user_ban_calls.append(username)
        self.banned_users.add(username)

    def unban_user(self, username):
        self.user_unban_calls.append(username)
        self.banned_users.discard(username)

    def is_user_ip_banned(self, username=None, ip_address=None):
        return bool(
            (ip_address and ip_address in self.banned_ips)
            or username in self.banned_ips.values()
        )

    def ban_user_ip(self, username=None, ip_address=None):
        self.ip_ban_calls.append((username, ip_address))
        self.banned_ips[ip_address or f"? ({username})"] = username

    def unban_user_ip(self, username=None, ip_address=None):
        self.ip_unban_calls.append((username, ip_address))

        if ip_address:
            self.banned_ips.pop(ip_address, None)
            return

        for saved_ip, saved_username in list(self.banned_ips.items()):
            if saved_username == username:
                del self.banned_ips[saved_ip]


class FakeUploads:
    def __init__(self):
        self.active_users = {}
        self.queued_users = {}
        self.failed_users = {}
        self.clear_calls = []

    def clear_uploads(self, uploads=None, denied_message=None):
        uploads = list(uploads or [])
        self.clear_calls.append((uploads, denied_message))

        for collection in (
            self.active_users,
            self.queued_users,
            self.failed_users,
        ):
            for username, user_uploads in list(collection.items()):
                for key, upload in list(user_uploads.items()):
                    if upload in uploads:
                        del user_uploads[key]
                if not user_uploads:
                    del collection[username]


class FakeCore:
    def __init__(self):
        self.network_filter = FakeNetworkFilter()
        self.users = types.SimpleNamespace(addresses={})
        self.buddies = types.SimpleNamespace(users={})
        self.uploads = FakeUploads()


def load_plugin_module():
    plugin_system = types.ModuleType("pynicotine.pluginsystem")
    plugin_system.BasePlugin = FakeBasePlugin
    pynicotine = types.ModuleType("pynicotine")
    pynicotine.pluginsystem = plugin_system

    sys.modules["pynicotine"] = pynicotine
    sys.modules["pynicotine.pluginsystem"] = plugin_system

    plugin_path = Path(__file__).parent.parent / "__init__.py"
    spec = importlib.util.spec_from_file_location("daily_download_limit_under_test", plugin_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLUGIN_MODULE = load_plugin_module()


class DailyDownloadLimitTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.plugin = PLUGIN_MODULE.Plugin()
        self.plugin.path = self.temporary_directory.name
        self.plugin.core = FakeCore()
        self.plugin.core.users.addresses["alice"] = ("203.0.113.10", 2234)
        self.plugin.loaded_notification()

    def tearDown(self):
        self.plugin.disable()
        self.temporary_directory.cleanup()

    def finish_upload(self, username="alice"):
        self.plugin.upload_finished_notification(
            username,
            "Music\\track.flac",
            "C:\\Shares\\Music\\track.flac",
        )

    def test_twenty_files_are_allowed_and_twenty_first_bans_user_and_ip(self):
        for _ in range(20):
            self.finish_upload()

        self.assertEqual([], self.plugin.core.network_filter.user_ban_calls)
        self.assertEqual([], self.plugin.core.network_filter.ip_ban_calls)

        self.finish_upload()

        self.assertEqual(["alice"], self.plugin.core.network_filter.user_ban_calls)
        self.assertEqual(
            [("alice", "203.0.113.10")],
            self.plugin.core.network_filter.ip_ban_calls,
        )

    def test_counts_survive_plugin_restart(self):
        for _ in range(20):
            self.finish_upload()

        restarted_plugin = PLUGIN_MODULE.Plugin()
        restarted_plugin.path = self.temporary_directory.name
        restarted_plugin.core = FakeCore()
        restarted_plugin.core.users.addresses["alice"] = ("203.0.113.10", 2234)
        restarted_plugin.loaded_notification()
        restarted_plugin.upload_finished_notification(
            "alice",
            "Music\\track-21.flac",
            "C:\\Shares\\Music\\track-21.flac",
        )

        self.assertEqual(["alice"], restarted_plugin.core.network_filter.user_ban_calls)
        self.assertEqual(
            [("alice", "203.0.113.10")],
            restarted_plugin.core.network_filter.ip_ban_calls,
        )

    def test_buddies_can_be_exempted(self):
        self.plugin.settings["exempt_buddies"] = True
        self.plugin.core.buddies.users = {"alice"}

        for _ in range(25):
            self.finish_upload()

        self.assertEqual([], self.plugin.core.network_filter.user_ban_calls)
        state_path = Path(self.temporary_directory.name) / self.plugin.STATE_FILENAME
        self.assertFalse(state_path.exists())

    def test_counter_resets_on_a_new_local_day(self):
        for _ in range(20):
            self.finish_upload()

        self.plugin._today = lambda: "2099-01-02"
        self.finish_upload()

        state_path = Path(self.temporary_directory.name) / self.plugin.STATE_FILENAME
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("2099-01-02", state["date"])
        self.assertEqual(1, state["users"]["alice"]["count"])
        self.assertEqual([], self.plugin.core.network_filter.user_ban_calls)

    def test_state_save_overwrites_a_stale_fixed_temporary_file(self):
        temporary_path = (
            Path(self.temporary_directory.name)
            / ".daily_download_limit_state.tmp"
        )
        temporary_path.write_text("stale state", encoding="utf-8")

        self.finish_upload()

        state_path = Path(self.temporary_directory.name) / self.plugin.STATE_FILENAME
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(1, state["users"]["alice"]["count"])
        self.assertFalse(temporary_path.exists())

    def test_optional_auto_unban_removes_plugin_created_bans_after_x_days(self):
        start = datetime(2099, 1, 1, 12, tzinfo=timezone.utc)
        self.plugin.settings["auto_unban"] = True
        self.plugin.settings["unban_after_days"] = 3
        self.plugin._now_utc = lambda: start

        for _ in range(21):
            self.finish_upload()

        with self.plugin._state_lock:
            self.plugin._cancel_auto_unban_timer_locked()

        self.plugin._now_utc = lambda: start + timedelta(days=3, seconds=1)
        self.plugin._auto_unban_timer_callback()

        network_filter = self.plugin.core.network_filter
        self.assertEqual(["alice"], network_filter.user_unban_calls)
        self.assertEqual([("alice", None)], network_filter.ip_unban_calls)
        self.assertNotIn("alice", network_filter.banned_users)
        self.assertEqual({}, network_filter.banned_ips)
        self.assertNotIn("alice", self.plugin._state["managed_bans"])

    def test_auto_unban_does_not_remove_preexisting_manual_bans(self):
        network_filter = self.plugin.core.network_filter
        network_filter.banned_users.add("alice")
        network_filter.banned_ips["203.0.113.10"] = "alice"

        self.plugin.settings["auto_unban"] = True
        self.plugin.settings["unban_after_days"] = 1

        for _ in range(21):
            self.finish_upload()

        self.assertEqual({}, self.plugin._state["managed_bans"])
        self.assertEqual([], network_filter.user_unban_calls)
        self.assertEqual([], network_filter.ip_unban_calls)

    def test_auto_unban_ledger_survives_plugin_restart(self):
        start = datetime(2099, 1, 1, 12, tzinfo=timezone.utc)
        self.plugin._now_utc = lambda: start

        for _ in range(21):
            self.finish_upload()

        restarted_plugin = PLUGIN_MODULE.Plugin()
        restarted_plugin.path = self.temporary_directory.name
        restarted_plugin.core = FakeCore()
        restarted_filter = restarted_plugin.core.network_filter
        restarted_filter.banned_users.add("alice")
        restarted_filter.banned_ips["203.0.113.10"] = "alice"
        restarted_plugin.settings["auto_unban"] = True
        restarted_plugin.settings["unban_after_days"] = 2
        restarted_plugin._now_utc = lambda: start + timedelta(days=2, seconds=1)

        try:
            restarted_plugin.loaded_notification()
            self.assertEqual(["alice"], restarted_filter.user_unban_calls)
            self.assertEqual([("alice", None)], restarted_filter.ip_unban_calls)
            self.assertNotIn("alice", restarted_plugin._state["managed_bans"])
        finally:
            restarted_plugin.disable()

    def test_enabling_auto_unban_after_a_ban_is_respected(self):
        start = datetime(2099, 1, 1, 12, tzinfo=timezone.utc)
        self.plugin._now_utc = lambda: start

        for _ in range(21):
            self.finish_upload()

        with self.plugin._state_lock:
            self.plugin._cancel_auto_unban_timer_locked()

        self.plugin.settings["auto_unban"] = True
        self.plugin.settings["unban_after_days"] = 1
        self.plugin._now_utc = lambda: start + timedelta(days=1, seconds=1)
        self.plugin._auto_unban_timer_callback()

        network_filter = self.plugin.core.network_filter
        self.assertEqual(["alice"], network_filter.user_unban_calls)
        self.assertEqual([("alice", None)], network_filter.ip_unban_calls)

    def test_plugin_managed_bans_survive_daily_counter_rollover(self):
        for _ in range(21):
            self.finish_upload()

        self.assertIn("alice", self.plugin._state["managed_bans"])

        self.plugin._today = lambda: "2099-01-02"
        self.plugin.core.users.addresses["bob"] = ("203.0.113.11", 2234)
        self.finish_upload("bob")

        self.assertEqual(1, self.plugin._state["users"]["bob"]["count"])
        self.assertIn("alice", self.plugin._state["managed_bans"])

    def test_remaining_user_uploads_are_cleared_after_the_ban(self):
        alice_active = types.SimpleNamespace(username="alice")
        alice_queued = types.SimpleNamespace(username="alice")
        alice_failed = types.SimpleNamespace(username="alice")
        bob_queued = types.SimpleNamespace(username="bob")
        uploads = self.plugin.core.uploads
        uploads.active_users["alice"] = {"active": alice_active}
        uploads.queued_users["alice"] = {"queued": alice_queued}
        uploads.failed_users["alice"] = {"failed": alice_failed}
        uploads.queued_users["bob"] = {"queued": bob_queued}

        for _ in range(21):
            self.finish_upload()

        cleared_uploads, denied_message = uploads.clear_calls[-1]
        self.assertEqual(
            {id(alice_active), id(alice_queued), id(alice_failed)},
            {id(upload) for upload in cleared_uploads},
        )
        self.assertEqual("Banned", denied_message)
        self.assertNotIn("alice", uploads.active_users)
        self.assertNotIn("alice", uploads.queued_users)
        self.assertNotIn("alice", uploads.failed_users)
        self.assertIn("bob", uploads.queued_users)

    def test_optional_limit_message_includes_scheduled_unban_time(self):
        start = datetime(2099, 1, 1, 12, tzinfo=timezone.utc)
        self.plugin._now_utc = lambda: start
        self.plugin.settings["send_limit_message"] = True
        self.plugin.settings["auto_unban"] = True
        self.plugin.settings["unban_after_days"] = 3

        for _ in range(22):
            self.finish_upload()

        self.assertEqual(1, len(self.plugin.private_messages))
        username, message, show_ui, switch_page = self.plugin.private_messages[0]
        self.assertEqual("alice", username)
        self.assertIn("daily download limit of 20 files", message)
        self.assertIn("remaining queued downloads were cancelled", message)
        self.assertIn("2099-01-04 12:00 UTC", message)
        self.assertFalse(show_ui)
        self.assertFalse(switch_page)


if __name__ == "__main__":
    unittest.main()
