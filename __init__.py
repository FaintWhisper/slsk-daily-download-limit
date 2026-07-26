# SPDX-License-Identifier: MIT

"""Nicotine+ plugin that enforces a per-user daily download limit."""

import json
import os
import tempfile

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from threading import RLock
from threading import Timer

from pynicotine.pluginsystem import BasePlugin


class Plugin(BasePlugin):

    STATE_FILENAME = "daily_download_limit_state.json"
    STATE_VERSION = 2
    AUTO_UNBAN_CHECK_SECONDS = 3600

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.settings = {
            "daily_file_limit": 20,
            "ban_username": True,
            "ban_ip_address": True,
            "exempt_buddies": False,
            "send_limit_message": False,
            "limit_message": (
                "You have exceeded my daily download limit of %limit% files. "
                "%ban_notice% %unban_notice%"
            ),
            "auto_unban": False,
            "unban_after_days": 7,
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
            "send_limit_message": {
                "description": "Send a private message when a user exceeds the limit",
                "group": "Automatic Message",
                "type": "bool",
            },
            "limit_message": {
                "description": (
                    "Private message sent when the limit is exceeded. Placeholders: "
                    "%user%, %limit%, %count%, %ban_notice%, %unban_notice%, "
                    "%unban_days%, %unban_at%"
                ),
                "group": "Automatic Message",
                "type": "textview",
            },
            "auto_unban": {
                "description": "Automatically remove bans created by this plugin",
                "group": "Automatic Unban",
                "type": "bool",
            },
            "unban_after_days": {
                "description": "Remove plugin-created bans after this many 24-hour days:",
                "group": "Automatic Unban",
                "type": "int",
                "minimum": 1,
                "maximum": 36500,
            },
        }

        self._state_lock = RLock()
        self._state = self._new_state()
        self._auto_unban_timer = None

    @staticmethod
    def _today():
        """Return the local calendar date used for the daily window."""
        return datetime.now().astimezone().date().isoformat()

    @staticmethod
    def _now_utc():
        return datetime.now(timezone.utc)

    def _new_state(self, managed_bans=None):
        return {
            "version": self.STATE_VERSION,
            "date": self._today(),
            "users": {},
            "managed_bans": managed_bans or {},
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

        try:
            unban_after_days = int(self.settings.get("unban_after_days", 7))
        except (TypeError, ValueError):
            unban_after_days = 7

        self.settings["unban_after_days"] = min(
            self.metasettings["unban_after_days"]["maximum"],
            max(
                self.metasettings["unban_after_days"]["minimum"],
                unban_after_days,
            ),
        )

        with self._state_lock:
            self._load_state_locked()
            if self._process_expired_bans_locked():
                self._save_state_locked()
            self._schedule_auto_unban_locked()

        self.log(
            "Loaded. Users may download %d completed files per local calendar day; "
            "file %d triggers the configured bans.",
            (
                self.settings["daily_file_limit"],
                self.settings["daily_file_limit"] + 1,
            ),
        )

        if self.settings.get("auto_unban", False):
            self.log(
                "Automatic unban is enabled after %d days for bans created by this plugin.",
                self.settings["unban_after_days"],
            )

    def disable(self):
        with self._state_lock:
            self._cancel_auto_unban_timer_locked()

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

        if not isinstance(raw_state, dict):
            self._state = self._new_state()
            return

        clean_users = {}
        raw_users = (
            raw_state.get("users", {})
            if raw_state.get("date") == self._today()
            else {}
        )

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

        clean_managed_bans = {}
        raw_managed_bans = raw_state.get("managed_bans", {})

        if isinstance(raw_managed_bans, dict):
            for username, raw_record in raw_managed_bans.items():
                if not isinstance(username, str) or not isinstance(raw_record, dict):
                    continue

                banned_at = raw_record.get("banned_at")
                if self._parse_utc_timestamp(banned_at) is None:
                    continue

                username_ban = bool(raw_record.get("username", False))
                ip_ban = bool(raw_record.get("ip", False))
                if not username_ban and not ip_ban:
                    continue

                clean_managed_bans[username] = {
                    "banned_at": banned_at,
                    "username": username_ban,
                    "ip": ip_ban,
                }

        self._state = {
            "version": self.STATE_VERSION,
            "date": self._today(),
            "users": clean_users,
            "managed_bans": clean_managed_bans,
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

        self._state = self._new_state(
            managed_bans=self._state.get("managed_bans", {}),
        )
        self.log("Started a new daily download-count window for %s.", today)

    @staticmethod
    def _parse_utc_timestamp(value):
        if not isinstance(value, str):
            return None

        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        return timestamp.astimezone(timezone.utc)

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

        if not isinstance(address, (list, tuple)) or not address:
            return ""

        ip_address = address[0]
        return ip_address if isinstance(ip_address, str) else ""

    def _enforce_bans(self, username, ip_address):
        actions = []
        ban_active = False
        managed_components = {
            "username": False,
            "ip": False,
        }
        network_filter = self.core.network_filter

        if self.settings.get("ban_username", True):
            try:
                is_username_banned = network_filter.is_user_banned(username)
                if not is_username_banned:
                    network_filter.ban_user(username)
                    actions.append("username banned")
                    managed_components["username"] = True
                ban_active = True
            except Exception as error:
                self.log("Could not ban username %s: %s", (username, error))

        if self.settings.get("ban_ip_address", True):
            try:
                is_ip_banned = network_filter.is_user_ip_banned(
                    username=username,
                    ip_address=ip_address or None,
                )

                if not is_ip_banned:
                    network_filter.ban_user_ip(
                        username=username,
                        ip_address=ip_address or None,
                    )
                    actions.append(
                        "IP banned" if ip_address else "IP ban requested"
                    )
                    managed_components["ip"] = True
                ban_active = True

            except Exception as error:
                self.log("Could not ban the IP address for %s: %s", (username, error))

        return actions, managed_components, ban_active

    def _get_remaining_user_uploads(self, username):
        try:
            uploads = self.core.uploads
        except AttributeError:
            return []

        remaining_uploads = []
        seen_uploads = set()

        for collection_name in ("active_users", "queued_users", "failed_users"):
            collection = getattr(uploads, collection_name, {})
            try:
                user_uploads = collection.get(username, {})
                values = user_uploads.values()
            except AttributeError:
                continue

            for upload in list(values):
                upload_id = id(upload)
                if upload_id in seen_uploads:
                    continue

                seen_uploads.add(upload_id)
                remaining_uploads.append(upload)

        return remaining_uploads

    def _cancel_remaining_uploads_after_ban(self, username):
        remaining_uploads = self._get_remaining_user_uploads(username)
        if not remaining_uploads:
            return True

        try:
            self.core.uploads.clear_uploads(
                uploads=remaining_uploads,
                denied_message="Banned",
            )
        except Exception as error:
            self.log(
                "Could not clear remaining uploads for banned user %s: %s",
                (username, error),
            )
            return False

        still_remaining = self._get_remaining_user_uploads(username)
        if still_remaining:
            self.log(
                "%d uploads still remain for banned user %s after cancellation.",
                (len(still_remaining), username),
            )
            return False

        self.log(
            "Cleared %d remaining queued, active, or failed uploads for banned user %s.",
            (len(remaining_uploads), username),
        )
        return True

    def _get_scheduled_unban_time_locked(self, username):
        if not self.settings.get("auto_unban", False):
            return None

        managed_bans = self._state.get("managed_bans", {})
        managed_record = managed_bans.get(username)
        if not managed_record:
            return None

        banned_at = self._parse_utc_timestamp(managed_record.get("banned_at"))
        if banned_at is None:
            return None

        return banned_at + timedelta(days=self.settings["unban_after_days"])

    def _send_limit_message_locked(
        self,
        username,
        count,
        ban_active,
        queue_cleared,
    ):
        if not self.settings.get("send_limit_message", False):
            return

        message = self.settings.get("limit_message", "")
        if not isinstance(message, str) or not message.strip():
            return

        scheduled_unban = self._get_scheduled_unban_time_locked(username)
        if scheduled_unban is not None:
            unban_at = scheduled_unban.astimezone(timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            unban_days = str(self.settings["unban_after_days"])
            unban_notice = f"Automatic unban is scheduled for {unban_at}."
        else:
            unban_at = "not scheduled"
            unban_days = "not scheduled"
            unban_notice = "Automatic unban is not scheduled."

        if ban_active and queue_cleared:
            ban_notice = (
                "You have been banned, and any remaining queued downloads "
                "were cancelled."
            )
        elif ban_active:
            ban_notice = (
                "You have been banned, but some queued downloads could not "
                "be cancelled."
            )
        else:
            ban_notice = "No automatic ban action is enabled."

        replacements = {
            "%user%": username,
            "%limit%": str(self.settings["daily_file_limit"]),
            "%count%": str(count),
            "%ban_notice%": ban_notice,
            "%unban_notice%": unban_notice,
            "%unban_days%": unban_days,
            "%unban_at%": unban_at,
        }

        for placeholder, value in replacements.items():
            message = message.replace(placeholder, value)

        for line in message.splitlines():
            line = line.strip()
            if not line:
                continue

            try:
                self.send_private(
                    username,
                    line,
                    show_ui=False,
                    switch_page=False,
                )
            except Exception as error:
                self.log(
                    "Could not send the limit message to %s: %s",
                    (username, error),
                )
                return

    def _record_managed_ban_locked(self, username, managed_components):
        if not any(managed_components.values()):
            return False

        managed_bans = self._state.setdefault("managed_bans", {})
        record = managed_bans.get(username)

        if record is None:
            record = {
                "banned_at": self._now_utc().isoformat(),
                "username": False,
                "ip": False,
            }
            managed_bans[username] = record

        record["username"] = (
            record.get("username", False)
            or managed_components["username"]
        )
        record["ip"] = (
            record.get("ip", False)
            or managed_components["ip"]
        )
        return True

    def _process_expired_bans_locked(self):
        if not self.settings.get("auto_unban", False):
            return False

        managed_bans = self._state.get("managed_bans", {})
        if not managed_bans:
            return False

        cutoff = self._now_utc() - timedelta(
            days=self.settings["unban_after_days"],
        )
        network_filter = self.core.network_filter
        state_changed = False

        for username, record in list(managed_bans.items()):
            banned_at = self._parse_utc_timestamp(record.get("banned_at"))
            if banned_at is None or banned_at > cutoff:
                continue

            actions = []

            if record.get("username", False):
                try:
                    if network_filter.is_user_banned(username):
                        network_filter.unban_user(username)
                        actions.append("username unbanned")
                    record["username"] = False
                    state_changed = True
                except Exception as error:
                    self.log("Could not auto-unban username %s: %s", (username, error))

            if record.get("ip", False):
                try:
                    if network_filter.is_user_ip_banned(username=username):
                        network_filter.unban_user_ip(username=username)
                        actions.append("IP unbanned")
                    record["ip"] = False
                    state_changed = True
                except Exception as error:
                    self.log("Could not auto-unban the IP for %s: %s", (username, error))

            if not record.get("username", False) and not record.get("ip", False):
                del managed_bans[username]
                state_changed = True

                action_text = ", ".join(actions) if actions else "bans already absent"
                self.log(
                    "Automatic unban period expired for %s (%s).",
                    (username, action_text),
                )

        return state_changed

    def _cancel_auto_unban_timer_locked(self):
        timer = self._auto_unban_timer
        self._auto_unban_timer = None

        if timer is not None:
            timer.cancel()

    def _schedule_auto_unban_locked(self):
        self._cancel_auto_unban_timer_locked()

        managed_bans = self._state.get("managed_bans", {})
        if not managed_bans:
            return

        now = self._now_utc()
        due_times = []

        if self.settings.get("auto_unban", False):
            for record in managed_bans.values():
                banned_at = self._parse_utc_timestamp(record.get("banned_at"))
                if banned_at is not None:
                    due_times.append(
                        banned_at + timedelta(days=self.settings["unban_after_days"])
                    )

        delay = self.AUTO_UNBAN_CHECK_SECONDS

        if due_times:
            remaining = (min(due_times) - now).total_seconds()
            if remaining > 0:
                delay = min(delay, remaining)

        timer = Timer(max(1, delay), self._auto_unban_timer_callback)
        timer.daemon = True
        self._auto_unban_timer = timer
        timer.start()

    def _auto_unban_timer_callback(self):
        with self._state_lock:
            self._auto_unban_timer = None

            if self._process_expired_bans_locked():
                self._save_state_locked()

            self._schedule_auto_unban_locked()

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

            record["count"] += 1
            if ip_address:
                record["last_ip"] = ip_address

            count = record["count"]
            limit = self.settings["daily_file_limit"]

            if count > limit:
                actions, managed_components, ban_active = self._enforce_bans(
                    user,
                    ip_address,
                )
                first_enforcement = not record.get("ban_applied", False)
                record["ban_applied"] = True
                managed_ban_added = self._record_managed_ban_locked(
                    user,
                    managed_components,
                )

                queue_cleared = (
                    self._cancel_remaining_uploads_after_ban(user)
                    if ban_active
                    else True
                )

                if first_enforcement or actions:
                    action_text = ", ".join(actions) if actions else "already banned"
                    self.log(
                        "Daily limit exceeded by %s after %d completed files (%s).",
                        (user, count, action_text),
                    )

                if first_enforcement:
                    self._send_limit_message_locked(
                        user,
                        count,
                        ban_active,
                        queue_cleared,
                    )

                if managed_ban_added:
                    self._schedule_auto_unban_locked()

            self._save_state_locked()
