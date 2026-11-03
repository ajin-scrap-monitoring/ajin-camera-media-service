# Guarded SD storage and reversible migration

The SD variant keeps the container root `/var/lib/camera-media` unchanged. The
suggested host destination is `/srv/ajin-camera-storage/sd/camera-media`, beneath
a second stable-UUID mount of the existing card at `/srv/ajin-camera-storage/sd`.
The existing `/srv/scrap-monitoring/media` mount remains untouched. Only the
dedicated media-only parent `/srv/ajin-camera-storage` is shared with the media
container. Existing scraper files and `lost+found` remain outside the service root.
No command formats the card. Existing
SSD footage, SQLite IDs, relative paths and policies are retained.

## Identity and loss detection

Development defaults leave the guard disabled. Production sets both
`MEDIA_EXPECTED_FS_UUID` and `MEDIA_EXPECTED_DEVICE` (Linux decimal `major:minor`).
Obtain both from the *same actual UUID block device*, never from a guessed device
number or a marker copied without verification. The Linux host preflight calls
`blkid -U`, verifies block ancestry (default `mmcblk0`), actual `st_dev`, ext4 mount,
capacity and write permissions. Run it again before every container creation or
restart after reboot; install it as the deployment unit's `ExecStartPre` with the
verified UUID and exact target path. Persist the existing SD mount by UUID in the
host mount configuration before enabling the deployment unit. The operating preflight depends on the existing stable-UUID mount and establishes the dedicated second mount itself. Order storage preparation before `docker.service` and the media unit; do not require the card globally for unrelated Docker services. A missing SD must fail media startup rather than create a root-filesystem directory.

The runtime checks the UUID marker together with `/proc/self/mountinfo`, kernel
device identity, ext4, read-only flags and process write permissions before root
creation/SQLite opening and on guarded write/admission paths. It pins mount IDs;
missing/wrong/remounted media latches failure until restart. The marker alone is
insufficient. A guard failure is an unavailable storage condition, regardless of
continued JPEG receipt.

A normal Docker bind can keep a detached filesystem alive after the host unmounts
it. Therefore `deploy/compose.sd.yaml` also binds the *parent* directory read-only
with `rslave` propagation. Set `MEDIA_WITNESS_PARENT` to the dedicated media-only
parent `/srv/ajin-camera-storage`; do not bind a parent containing deployment
credentials or unrelated application files. Its `/run/media-host/sd` witness must remain a real
slave-propagated SD submount on the same device. Losing or replacing the host
submount then fails checks even if `/var/lib/camera-media` still pins the old SD.
The guard rejects private/unpropagated witness mounts. Inspect host propagation
with `findmnt -o TARGET,SOURCE,FSTYPE,PROPAGATION /srv/ajin-camera-storage/sd`.
Prepare a shared parent in a controlled maintenance window if necessary, then
recreate the container and verify the witness has `master:` in its mountinfo.
Changing propagation must be persisted in the host deployment unit; merely
setting Compose `rslave` does not make a private host mount shared. Test loss on a
dedicated disposable mount fixture, never by unmounting this shared operating SD.

`MEDIA_IDENTITY_PATH` optionally locates the marker in an ancestor on the same
filesystem when the guarded root is a subdirectory. Production ordinarily leaves
it empty, placing the marker in the service root. Both binds disable automatic
host-directory creation.

## Copy and verification

Run Python 3.13 or newer on the Linux Docker host as root (the standard-library migration tool was also verified with host Python 3.14),
with `PYTHONPATH=src`. These examples use placeholders; substitute the inspected
UUID. Keep runtime env files and tokens private. The tool prints aggregate counts,
capacity and identity only, and never prints Docker inspection or DB values.

1. Back up Compose/runtime configuration and a consistent SQLite snapshot. Record
   exact current bind source, numeric ownership, file counts and directory sizes.
   Create only the dedicated empty target directory on the confirmed SD. Set a
   capacity budget that leaves archive workspace and minimum free space; a
   retention age is not a promise that 30 days fit on a 62.5 GB card.
2. Inspect the target and initialize its marker once:

   ```sh
   PYTHONPATH=src python tools/media_storage_migrate.py inspect --target /srv/ajin-camera-storage/sd/camera-media --expected-uuid UUID
   PYTHONPATH=src python tools/media_storage_migrate.py initialize --target /srv/ajin-camera-storage/sd/camera-media --expected-uuid UUID
   ```

3. Stop only the media service and keep it stopped throughout final copy and
   verification. Disable any automatic restart/deployment action for this window.
   The tool requires a stopped container whose source bind matches exactly,
   rejects running containers referencing either tree, and scans host open files
   and memory maps. Failure to inspect a process fails closed. It does not stop
   services on your behalf. Do not run other writers against either tree.

   ```sh
   PYTHONPATH=src python tools/media_storage_migrate.py copy --source /var/lib/ajin-camera-media --target /srv/ajin-camera-storage/sd/camera-media --expected-uuid UUID --stopped-container ajin-camera-media-service --manifest /root/media-migration-verified.json
   PYTHONPATH=src python tools/media_storage_migrate.py verify --source /var/lib/ajin-camera-media --target /srv/ajin-camera-storage/sd/camera-media --expected-uuid UUID --stopped-container ajin-camera-media-service --manifest /root/media-migration-verified.json
   ```

   The manifest includes every regular file, hidden DB and WAL/SHM, SHA-256 content,
   size, mtime, ownership and mode, plus directory permissions. SQLite integrity
   checks and table counts run against disposable DB/WAL/SHM copies, leaving the
   source untouched. Source and destination manifests must agree; source is
   checked again after copy. Linux destination operations pin directory file
   descriptors and refuse symlink traversal, nested filesystems and special files.
   Files publish atomically without overwriting any existing destination file.
   An operator may precopy only immutable, closed media assets while live,
   preserving their exact numeric owners, modes and file mtimes. Final copy
   rehashes those existing files and reuses exact matches. Do not precopy mutable
   SQLite/WAL/SHM, active day masters, active TS, or temporary files: changed
   destination files are deliberately refused instead of overwritten. The CLI
   itself always requires a stopped source; there is no unsafe live-copy flag.
   Resume an interruption with a fresh external manifest path: already matching
   files are reused, mismatching or additional files require investigation.
   `.camera-media-migration-staging` is a reserved noncatalog staging directory;
   an abrupt kill may leave incomplete temporary files there. They are excluded
   from the verified data manifest and retained. The tool never deletes footage
   or cleans a preexisting staging directory. Directory mtimes/ctime are not
   promised; file content, mtime, POSIX modes and numeric owner/group are verified.
4. Set `MEDIA_DATA_DIR` to the SD child directory, `MEDIA_WITNESS_PARENT` to the
   dedicated media-only shared parent and the verified UUID/device in
   the Compose substitution environment. Use both Compose files, preserving
   existing image, env secrets, networks, ports and resource limits. Keep the
   container path unchanged. Run the host preflight, start, and inspect health.
5. Verify old catalog IDs, new physical SD writes, independent clip downloads,
   HEAD/Range, daily recording/HLS, and metadata after restart. Repeat identity
   and ownership checks after restart. Record the exact point at which new SD
   writes begin. Keep SSD originals and the verified manifest.

## Rollback

Before any new SD writes, stop the service, restore the saved SSD bind/config and
restart the tested previous image; the original SSD tree is unchanged. After new
SD writes, do **not** copy the old SSD tree over the new one, or switch back and
discard the new catalog. Stop writers, preserve both trees and SQLite snapshots,
compare manifests/IDs and reconcile additions in a separate reviewed copy before
switching. This tool intentionally refuses conflicting destination files. There
is no automatic deletion, move, fallback to SSD, or destructive rollback command.


## Operating deployment — 2026-11-03

Current image: `ajin-camera-media-service:8d5d584`. The application image remains Python3.13. Host preflight/migration runs with the standard-library Python3.14 installation. The verified card is ext4 `/dev/mmcblk0p1`, kernel179:1, UUID `1e54ae89-0a90-479f-a47d-59e64d223b29`, capacity62,522,208,256bytes. Its reported product name is SR64G; a continuous-write endurance grade was not established.

| Data | Physical SD location under `/srv/ajin-camera-storage/sd/camera-media` |
|---|---|
| Received JPEG buffer | `buffer/` |
| Daily TS / verified whole MP4 | `daily/` / `daily-exports/` |
| Independent event/range MP4 | `clips/` |
| HLS playback cache | `daily-playback/` |
| In-progress output/concat manifests | Beside the owned output in those same directories; included in budgets and cleaned on completion/failure/recovery |
| Catalogue and measured information | `catalog.sqlite3` with WAL/SHM and private `media-info.sqlite3` |

Final frozen migration verified43,861files/29,911,141,866bytes and SQLite integrity. Old SSD `/var/lib/ajin-camera-media` is retained. Application path `/var/lib/camera-media`, IDs, existing deleted/expired states and policies were preserved. The RO rslave witness is `/run/media-host/sd` from the dedicated shared parent. Real service UID10002, root10002:10002/mode0750, new file device179:1 and service restart were verified. Host reboot/power-loss/endurance tests were not performed.

Managed units are `/etc/systemd/system/ajin-camera-media-storage.service` and `/etc/systemd/system/ajin-camera-media.service`; both enabled/active. The media unit always combines the original `/srv/scrap-monitoring/development/main-refresh-20261015/compose.media.yaml` and private `/home/kyuncho/ajin-camera-media-service/deploy-20261103-sd/compose.sd.runtime.yaml`, and waits for healthy. Use `sudo systemctl restart ajin-camera-media.service`; using the old base file alone would select the preserved SSD configuration. Preflight is `/usr/local/libexec/ajin-camera-media-storage-preflight` and uses the pinned1923d83 tools checkout. Do not remove that checkout without updating the managed dependency.

Current application budgets: total48GiB, daily42GiB, archive workspace10GiB, minimum filesystem free5GiB, JPEG2GiB. Workspace is reserved within admission accounting, not an extra promise beyond device capacity. The existing30-day retention setting is an age limit, not30days of guaranteed storage. Download completion does not delete or extend a file. API expiry and physical cleanup remain distinct. See the Nov03 integration contract for file-specific rules and VALIDATION.md for actual operating proofs, maintenance gaps and performance limits.
