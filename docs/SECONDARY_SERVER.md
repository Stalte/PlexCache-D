# Secondary Plex server (fork feature)

PlexCache-D can watch a second Plex server that uses the **same media folders**
as the primary one (for example two Plex containers on one Unraid box). Both
servers share one cache drive, one set of trackers, one mover exclusion list and
one eviction pass, so they never fight over files. Each server keeps its own
owner account and users.

## Setup

1. Back up your appdata folder (e.g. `/mnt/secondcache/plexcache`).
2. Point the container at `ghcr.io/stalte/plexcache-d:latest`.
3. Open **Settings > Secondary Server**, enter the second server's URL, click
   **Get Token** and sign in as that server's owner (or paste the token),
   then **Save Connection** and **Sync Users**.
4. Run once in dry-run mode and check the logs for `[SECONDARY]` lines.

The first save writes `plexcache_settings.pre-multiserver.json` next to the
settings file. Your existing settings, users and `/config/data` files are not
changed; the primary server keeps using them exactly as before.

## How it behaves

* Libraries are picked once (Settings > Libraries) and matched on the secondary
  server **by library name**. Unmatched names are logged and skipped there.
* Secondary users appear in trackers as `name@secondary` and in the UI with a
  server tag, so a friend on both servers is tracked once per server while
  their files are only cached once.
* Pins are resolved through the primary server. Because both servers see the
  same files, a pin protects the file for users of either server.
* If the secondary server is down or returns partial data, the run continues
  with the primary, keeps the secondary users' last known OnDeck files on cache,
  and skips move-back and eviction for that run. The failure is logged as an
  error so it shows on the dashboard.
* Either server's owner can log in to the web UI with Plex.
* The "Multi-User Support" switch on the Users tab applies to both servers.
