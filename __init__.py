# SPDX-License-Identifier: MIT

"""Nicotine+ plugin that enforces a per-user daily download limit."""

import json
import os
import tempfile

from datetime import datetime
from threading import RLock

from pynicotine.pluginsystem import BasePlugin


class Plugin(BasePlugin):

    STATE_FILENAME = "daily_download_limit_state.json"
    STATE_VERSION = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.settings = {
            "daily_file_limit": 20,
            "ban_username": True,
            "ban_ip_address": True,
            "exempt_buddies": False,
        }
        self.metasettings = {
            "daily_file_limit": {
                "description": (
                    "Completed files a user may download per local calendar day "
                    "(the next completed file triggers the ban):"
                ),
                "group": "Limit",
                "type": "int",
                "minimum": 1,
            },
            "ban_username": {
                "description": "Ban the Soulseek username when the limit is exceeded",
                "group": "Actions",
                "type": "bool",
            },
            "ban_ip_address": {
                "description": "Ban the user's current IP address when the limit is exceeded",
                "group": "Actions",
                "type": "bool",
            },
            "exempt_buddies": {
                "description": "Do not count downloads from users in the buddy list",
                "group": "Exceptions",
                "type": "bool",
            },
        }

        self._state_lock = RLock()
        self._state = self._new_state()

    @staticmethod
    def _today():
        """Return the local calendar date used for the daily window."""
        return datetime.now().astimezone().date().isoformat()

    def _new_state(self):
        return {
            "version": self.STATE_VERSION,
            "date": self._today(),
            "users": {},
        }

    def _get_state_path(self):
        plugin_path = self.path or os.path.dirname(os.path.realpath(__file__))
        return os.path.join(plugin_path, self.STATE_FILENAME)

    def loaded_notification(self):
        try:
            limit = int(self.settings.get("daily_file_limit", 20))
        except (TypeError, ValueError):
            limit = 20

        self.settings["daily_file_limit"] = max(
            self.metasettings["daily_file_limit"]["minimum"],
            limit,
        )

        with self._state_lock:
            self._load_state_locked()

        self.log(
            "Loaded. Users may download %d completed files per local calendar day; "
            "file %d triggers the configured bans.",
            (
                self.settings["daily_file_limit"],
                self.settings["daily_file_limit"] + 1,
            ),
        )

    def _load_state_locked(self):
        state_path = self._get_state_path()

        try:
            with open(state_path, "r", encoding="utf-8") as state_file:
                raw_state = json.load(state_file)

        except FileNotFoundError:
            self._state = self._new_state()
            return

        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            self.log("Could not read daily counter state; starting fresh: %s", error)
            self._state = self._new_state()
            return

        if not isinstance(raw_state, dict) or raw_state.get("date") != self._today():
            self._state = self._new_state()
            return

        clean_users = {}
        raw_users = raw_state.get("users", {})

        if isinstance(raw_users, dict):
            for username, raw_record in raw_users.items():
                if not isinstance(username, str) or not isinstance(raw_record, dict):
                    continue

                try:
                    count = max(0, int(raw_record.get("count", 0)))
                except (TypeError, ValueError):
                    count = 0

                last_ip = raw_record.get("last_ip")
                if not isinstance(last_ip, str):
                    last_ip = ""

                clean_users[username] = {
                    "count": count,
                    "last_ip": last_ip,
                    "ban_applied": bool(raw_record.get("ban_applied", False)),
                }

        self._state = {
            "version": self.STATE_VERSION,
            "date": self._today(),
            "users": clean_users,
        }

    def _save_state_locked(self):
        state_path = self._get_state_path()
        state_folder = os.path.dirname(state_path)
        temporary_path = None

        try:
            os.makedirs(state_folder, exist_ok=True)
            file_descriptor, temporary_path = tempfile.mkstemp(
                prefix=".daily_download_limit_",
                suffix=".tmp",
                dir=state_folder,
                text=True,
            )

            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as state_file:
                json.dump(
                    self._state,
                    state_file,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())

            os.replace(temporary_path, state_path)
            temporary_path = None

        except OSError as error:
            self.log("Could not save daily counter state: %s", error)

        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def _roll_over_day_locked(self):
        today = self._today()
        if self._state.get("date") == today:
            return

        self._state = self._new_state()
        self.log("Started a new daily download-count window for %s.", today)

    def _is_exempt_buddy(self, username):
        if not self.settings.get("exempt_buddies", False):
            return False

        try:
            return username in self.core.buddies.users
        except (AttributeError, TypeError):
            return False

    def _get_known_ip_address(self, username):
        try:
            address = self.core.users.addresses.get(username)
        except AttributeError:
            return ""

        if not address:
            return ""

        ip_address = address[0]
        return ip_address if isinstance(ip_address, str) else ""

    def _enforce_bans(self, username, ip_address, force_ip_ban=False):
        actions = []
        network_filter = self.core.network_filter

        if self.settings.get("ban_username", True):
            try:
                if not network_filter.is_user_banned(username):
                    network_filter.ban_user(username)
                    actions.append("username banned")
            except Exception as error:
                self.log("Could not ban username %s: %s", (username, error))

        if self.settings.get("ban_ip_address", True):
            try:
                is_ip_banned = network_filter.is_user_ip_banned(
                    username=username,
                    ip_address=ip_address or None,
                )

                if force_ip_ban or not is_ip_banned:
                    network_filter.ban_user_ip(
                        username=username,
                        ip_address=ip_address or None,
                    )
                    actions.append(
                        "IP banned" if ip_address else "IP ban requested"
                    )

            except Exception as error:
                self.log("Could not ban the IP address for %s: %s", (username, error))

        return actions

    def upload_finished_notification(self, user, virtual_path, real_path):
        """Count a file after another user finishes downloading it from us."""
        if not user or self._is_exempt_buddy(user):
            return

        with self._state_lock:
            self._roll_over_day_locked()

            users = self._state["users"]
            record = users.setdefault(
                user,
                {
                    "count": 0,
                    "last_ip": "",
                    "ban_applied": False,
                },
            )

            ip_address = self._get_known_ip_address(user)
            previous_ip = record.get("last_ip", "")

            record["count"] += 1
            if ip_address:
                record["last_ip"] = ip_address

            count = record["count"]
            limit = self.settings["daily_file_limit"]

            if count > limit:
                actions = self._enforce_bans(
                    user,
                    ip_address,
                    force_ip_ban=(
                        not record.get("ban_applied", False)
                        or bool(ip_address and ip_address != previous_ip)
                    ),
                )
                first_enforcement = not record.get("ban_applied", False)
                record["ban_applied"] = True

                if first_enforcement or actions:
                    action_text = ", ".join(actions) if actions else "already banned"
                    self.log(
                        "Daily limit exceeded by %s after %d completed files (%s).",
                        (user, count, action_text),
                    )

            self._save_state_locked()
