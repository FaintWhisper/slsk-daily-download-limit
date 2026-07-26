# Soulseek Daily Download Limit

A Nicotine+ plugin that limits how many files each Soulseek user may download
from you in one local calendar day. By default, a user may complete 20 file
downloads; when the 21st file completes, the plugin bans both the username and
its current IP address.

## Behavior

- Counts only files successfully downloaded from your shares.
- Maintains a separate counter for each Soulseek username.
- Allows exactly 20 completed files by default and bans on file 21.
- Resets counters when the machine's local calendar date changes.
- Persists the current day's counters across Nicotine+ restarts.
- Uses Nicotine+'s native username and IP ban lists.
- Can optionally exempt users in your buddy list.
- Can optionally remove plugin-created bans after a configurable number of days.

By default, bans remain in Nicotine+ until you remove them. Automatic unban is
opt-in and never removes bans that already existed before this plugin enforced
the download limit.

## Installation

### Nicotine+ 3.4.0 or newer

1. Download [daily_download_limit.zip](daily_download_limit.zip) from the repository.
2. In Nicotine+, open **Preferences → Plugins**.
3. Select **Install…** and choose the ZIP file.
4. Enable **Daily Download Limit**.

### Manual installation

Create a folder named `daily_download_limit` in your Nicotine+ user plugin
directory, then copy `__init__.py` and `PLUGININFO` into it.

- Windows: `%AppData%\Roaming\nicotine\plugins\daily_download_limit\`
- Linux and other Unix-like systems:
  `~/.local/share/nicotine/plugins/daily_download_limit/`

Restart Nicotine+ or reopen Preferences, then enable the plugin.

## Configuration

Open the plugin's settings in Nicotine+ to configure:

- **Daily file limit** — defaults to `20`. The following completed file
  triggers enforcement.
- **Ban username** — enabled by default.
- **Ban IP address** — enabled by default.
- **Exempt buddies** — disabled by default.
- **Automatically remove plugin-created bans** — disabled by default.
- **Unban after days** — defaults to `7` and uses 24-hour periods from the time
  the ban was applied.

The limit is literal: with the default value, file 20 is allowed and file 21
triggers the ban.

## State and privacy

The plugin creates `daily_download_limit_state.json` in its installed folder.
It stores the current date, usernames, completed-file counts, enforcement
status, the last IP address seen for each user, and timestamps for bans managed
by the optional automatic-unban feature. The state file is replaced atomically
after each completed transfer.

## Development

Run the test suite with:

```powershell
python -B -m unittest discover -s tests -v
```

The tests cover the default threshold, username/IP enforcement, persistence
across restarts, local-day rollover, buddy exemptions, timed automatic unban,
and protection for pre-existing manual bans.

## License

[MIT](LICENSE)
