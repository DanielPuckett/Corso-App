# Scheduled Prune: Spec

A daily job that checks every Corso backup group is fresh and healthy, thins old snapshots by retention policy, runs repository maintenance weekly, and emails a report. The checks are the main value; disk space is secondary.

Status: in use. Backlog cleanup done 2026-11-19 (10,283 of 11,711 snapshots deleted in 20.5 min; 1,428 kept across 11 groups). Two maintenance passes took the repo from 72 GiB to 67 GiB (5 GiB reclaimed, less than the ~9 GB estimated from the 14 MB/run measurement, which included index data later compacted).

## Background

- The NAS cron runs `m365backup.sh` at 00:30, 06:30, 12:30 and 18:30. Each run backs up OneDrive, Exchange and SharePoint for every user, so 2 users produce 6 snapshots per run, 24 per day.
- Chain of copies: Microsoft 365 → NAS Corso repo (`/mnt/corso/repo`) → backup server (nightly restic snapshot with 30 days of history; see restic-tui's PRUNE_SPEC.md, "Corso repo history") → mirror. Until 2026-11-26 the backup server copy was a nightly `rsync --delete`, so a bad delete reached every copy within a day; the restic history now gives 30 days to notice and roll back. The guards below and a first `--dry-run` remain the first line of protection.
- Measured cost: a back-to-back full run with no data changes added 14.1 MB (6 snapshots). Repo growth since April (63 → 72 GB in about 6½ months, about 1.4 GB/month) matches about 14 MB per run. Almost all growth is per-snapshot metadata (each backup stores a full details list of every item, and OneDrive has 184k files), not changed data. Fewer kept snapshots means less disk.
- Corso never runs maintenance on its own. Deleting a snapshot frees nothing until `repo maintenance` runs.

## Vocabulary

- **Group**: all snapshots of one protected resource (a user, or a SharePoint site) for one service (`exchange`, `onedrive`, `sharepoint`). Every rule and check works per group.
- **Successful snapshot**: status `Completed` (including `Completed (N errors…)`) or `No Data`. Anything else (`Failed`, and `In Progress` or `Status Unknown` left behind by a crashed run) is not successful.
- **Guard**: a precondition. If one fails, nothing is deleted and the report says FAIL.
- **Floor**: a rule that holds no matter what. It only blocks deletion and never raises an alert.

## Retention (defaults, adjustable)

| Age | Keep |
|---|---|
| under 7 days | everything |
| 7 days – 3 months | first successful snapshot of each day |
| over 3 months | first successful snapshot of each month, kept indefinitely |

- No maximum age. This is private data for two people who want long history ("I deleted an email a couple of years back, do you still have it?"). Monthly forever costs about 14 MB per month.
- Day and month boundaries use local time (`timezone:` in config), so "first of the day" is the 00:30 run. Corso reports times in UTC; the job converts.
- "First" means the earliest successful snapshot in the whole calendar bucket, so the bucket that straddles a tier boundary does not keep a different snapshot each day.
- Failed snapshots are never chosen as a bucket's representative, so they are deleted once they are older than 7 days.
- Expected effect: about 115 snapshots per group today (history starts April 2026), growing by one per month.

## Floors

- Always keep the newest 3 successful snapshots in every group.

## Guards

Checked before any deletion. If a guard fails, nothing is deleted.

- **Freshness**: the newest successful snapshot in each group is within 7h (6h cycle plus slack).
- **Status**: if the newest snapshot in a group is not successful (`Failed`, `In Progress`, `Status Unknown`), that's FAIL. If it is `Completed (N errors…)`, that's WARN.
- **Coverage**: every group that has ever existed is checked, so a user or service that silently stops being backed up goes stale and fails. Resources retired on purpose go in `retired_resources` (freshness skipped, retention still applies; a retired group with a fresh snapshot is WARN).
- **Deletion cap**: a run may delete at most 20% of all snapshots. `--no-cap` turns this off (initial cleanup only).
- **Canary** (`canaries` in config): a file written into Microsoft 365 from outside Corso. A laptop cron writes the time to `.backup_canary` in the root of the user's OneDrive at :15 past 00, 06, 12 and 18; the OneDrive client uploads it, and the :30 backup captures it. For the newest successful snapshot of that user's OneDrive, the job looks the file up with `backup details onedrive --backup <id> --file .backup_canary --json` (metadata only, nothing exported) and compares the file's modified time (from OneDrive's own record, so the laptop's clock doesn't matter) with the snapshot time:
  - not in the snapshot: **FAIL** on that group, deletes blocked (Corso did not capture the change);
  - changed more than `max_age` (default 26h) before the snapshot: **WARN** (laptop asleep or away, the OneDrive client not uploading, or Corso not picking up changes). 26h keeps overnight sleep quiet and still flags a laptop that has been off for a day;
  - otherwise a note, e.g. `canary ok: changed 16 min before snapshot …`.
  - Corso prints nothing when no item matches; that counts as missing. A canary whose resource has no backups at all is FAIL. `--resource` for someone else skips it.
  - Verified 2026-11-26 against the real repo: OK (16 min), missing (wrong file name: FAIL), stale (`max_age: 5m`: WARN).
- **Single runner**: the job takes a lock file (`/mnt/corso_tui/prune.lock`). If the lock is held, it exits with SKIPPED. See Maintenance for why.

## Maintenance

- Runs weekly on `maintenance_day` (default `sunday`), after the deletes, in the same run. `--maintenance` forces a pass on any day; `--no-maintenance` skips it.
- Command: the existing maintenance script, `scripts.maint` in config (`/mnt/corso_tui/scripts/m365maint.sh`). It holds the credentials and runs `repo maintenance --mode complete --force=true` with the same `--hostname corso-backup-server --user 1000:1000` as backups. Its exit code and the last lines of its output go into the report.
- `--force=true` rewrites the maintenance owner to the current `user@host` and runs regardless of ownership. It does not lower the 24-hour safety margin. It also removes protection against two maintenance runs overlapping on one repo, so:
  - the job's lock must be held for the whole run;
  - the maintenance script itself takes no lock, so don't run it by hand while the job might be running (around 06:30 to 07:30 on `maintenance_day`).
- Space is reclaimed with a delay, and the clock starts at a maintenance pass, not at the delete. A pass rewrites the data still in use and marks what is unused; only a pass at least 24h after that one removes it. So the first pass after deletes usually grows the repo (April: 143 → 174 → 63 GB over two passes a day apart; November: 72 → 79 GiB on pass 1, 26 min; 79 → 67 GiB on pass 2, 4 min). With daily deletes and a weekly pass, each week's pass removes what the previous week's pass marked.
- The report records repo size (`du -sb /mnt/corso/repo`) and free space on `/mnt/corso` before and after each pass.

## Running

- Script: `prune_job.py` in the corso-tui container (it already has the Docker socket and the stub). It reuses `corso.py` for listing and deleting.
- Snapshot listing: `backup list <service> --json`, falling back to the existing fixed-width parser if the JSON lacks a field the job needs. Verified on the first dry run.
- Deletion: `backup delete <service> --backups id1,id2,…` in batches of `delete_batch_size`.
- Trigger: `m365backup.sh` calls the job at the end of the 06:30 run, so it never overlaps a backup and finishes well before the 2 AM rsync. The start hour is recorded at the top of the script so a run that ends after 07:00 still counts:
  ```bash
  START_HOUR=$(date +%H)        # top of m365backup.sh
  # ... backups ...
  if [ "$START_HOUR" = "06" ]; then
    cd /home/user/corso-tui && /usr/bin/docker compose run --rm -T --entrypoint python corso-tui /app/prune_job.py >> /mnt/corso_tui/prune_cron.log 2>&1
  fi
  ```
  `-T` because there is no terminal. On `maintenance_day` the same run also does maintenance, so there is no separate maintenance trigger. Setting `maintenance_day: ""` turns the built-in maintenance off (then trigger it with `--maintenance`).
- Order per run: lock → list snapshots (all services) → plan → guards → delete → maintenance (if due) → report.
- Flags: `--dry-run`, `--no-cap`, `--no-email`, `--resource NAME` (one user or site), `--maintenance`, `--no-maintenance`.

## Report

- Written to `/mnt/corso_tui/prune_reports/YYYY-MM-DD_HHMMSS[-dry-run].txt` and `/mnt/corso_tui/last_prune.txt`.
- Every run writes `<same name>.deletes.txt` (planned deletes; the first line says whether they were deleted, a dry run, or blocked by a guard) and `<same name>.keeps.txt` (every kept snapshot with the reason: recent, daily, monthly or floor). Together they list every snapshot once.
- Each group in the report shows a breakdown, e.g. `kept: 28 recent, 85 daily, 21 monthly`.
- Subject: `[corso] FAIL 0 / WARN 0 / SKIPPED 0 / OK 11` (one count per group, plus maintenance on maintenance days).
- Summary first, sized for a phone: "Needs attention" lists each FAIL/WARN/SKIPPED finding as one short line, e.g. `WARN     me@example.com / onedrive: canary is stale`, `FAIL     sites/allcompany / sharepoint: stale 12.2h` (SharePoint site URLs shortened to their path, the root site as `root site`), or "Everything OK.", plus the maintenance result if a pass ran. Full reasons are in Details.
- Details per group: resource, service, snapshots kept and deleted, oldest and newest kept, newest snapshot's status and age.
- Email settings: `/mnt/corso_tui/.env` (same keys as restic-tui: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`, `SMTP_TO`).
- Exit code: 1 if anything is FAIL or the email failed, otherwise 0.

## Config (`/mnt/corso_tui/config.yaml`)

```yaml
scripts:
  stub: /mnt/corso_tui/scripts/m365stub.sh
  maint: /mnt/corso_tui/scripts/m365maint.sh   # run by the job on maintenance_day

prune:
  report_dir: /mnt/corso_tui/prune_reports
  last_report: /mnt/corso_tui/last_prune.txt
  env_file: /mnt/corso_tui/.env
  lock_file: /mnt/corso_tui/prune.lock
  repo_path: /mnt/corso/repo
  timezone: America/Toronto          # local day/month boundaries
  services: [exchange, onedrive, sharepoint]

  keep_last: 3
  max_delete_percent: 20
  fresh_within: 7h
  retired_resources: []

  keep_all_within: 7d
  tiers:
    - {older_than: 7d, keep: daily}
    - {older_than: 3M, keep: monthly}

  maintenance_day: sunday      # the daily job also runs scripts.maint on this day

  canaries:                    # files written into M365 from outside Corso
    - service: onedrive
      resource: me@example.com # or an alias from 'users:'
      file: .backup_canary     # written by a laptop cron at :15
      max_age: 26h             # older than this -> WARN
```

Durations: `m` (minutes), `h`, `d`, `w`, `M` (months), `y`. Lowercase `m` is minutes and capital `M` is months.

## Rollout

1. Check free space for the maintenance swell (about 72 GB plus 25%) on the NAS, the backup server and the mirror. The 2 AM rsync between the two passes copies the swollen repo.
2. `--dry-run --no-maintenance`, then review the report.
3. One run with `--no-cap --no-maintenance` to clear the backlog.
4. `--maintenance` (pass 1; repo may grow).
5. More than 24h after pass 1, `--maintenance` again (pass 2; space is reclaimed). Compare with the size before pass 1 (76.6 GB / 72 GiB); expect at least about 9 GB less.
6. Add the call to the end of the 06:30 `m365backup.sh` run.

## Deployment

- Add `prune_job.py` to the `COPY` line in `Dockerfile`.
- Add `tzdata` to `requirements.txt` (the slim image may lack zone data).
- Copy changed files to the NAS `~/corso-tui`, then `docker compose build`.
