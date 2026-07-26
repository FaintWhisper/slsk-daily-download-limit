import importlib.util
import json
import sys
import types
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory


class FakeBasePlugin:
    path = None
    core = None
    human_name = "Daily Download Limit"

    def __init__(self, *args, **kwargs):
        self.logs = []

    def log(self, message, args=None):
        self.logs.append((message, args))


class FakeNetworkFilter:
    def __init__(self):
        self.banned_users = set()
        self.banned_ips = {}
        self.user_ban_calls = []
        self.ip_ban_calls = []

    def is_user_banned(self, username):
        return username in self.banned_users

    def ban_user(self, username):
        self.user_ban_calls.append(username)
        self.banned_users.add(username)

    def is_user_ip_banned(self, username=None, ip_address=None):
        return bool(
            (ip_address and ip_address in self.banned_ips)
            or username in self.banned_ips.values()
        )

    def ban_user_ip(self, username=None, ip_address=None):
        self.ip_ban_calls.append((username, ip_address))
        self.banned_ips[ip_address or f"? ({username})"] = username


class FakeCore:
    def __init__(self):
        self.network_filter = FakeNetworkFilter()
        self.users = types.SimpleNamespace(addresses={})
        self.buddies = types.SimpleNamespace(users={})


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


if __name__ == "__main__":
    unittest.main()
