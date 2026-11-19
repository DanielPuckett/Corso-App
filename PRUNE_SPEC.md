# Scheduled Prune: Spec

A daily job that checks every Corso backup group is fresh and healthy, thins old snapshots by retention policy, runs repository maintenance weekly, and emails a report. The checks are the main value; disk space is secondary.

Status: built (`prune_job.py`), tested locally against simulated snapshots; not yet run against the real repo.

## Background

- The NAS cron runs `m365backup.sh` at 00:30, 06:30, 12:30 and 18:30. Each run backs up OneDrive, Exchange and SharePoint for every user, so 2 users produce 6 snapshots per run, 24 per day.
- Chain of copies: Microsoft 365 → NAS Corso repo (`/mnt/corso/repo`) → backup server (2 AM `rsync --delete` over SSHFS) → mirror (`rsync --delete`). Deletions on the NAS reach every copy within a day. No copy keeps older snapshots, so the guards below and a first `--dry-run` are the only protection against a bad delete.
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
- **Single runner**: the job takes a lock file (`/mnt/corso_tui/prune.lock`). If the lock is held, it exits with SKIPPED. See Maintenance for why.

## Maintenance

- Runs weekly on `maintenance_day` (default `sunday`), after the deletes, in the same run. `--maintenance` forces a pass on any day; `--no-maintenance` skips it.
- Command: the existing maintenance script, `scripts.maint` in config (`/mnt/corso_tui/scripts/m365maint.sh`). It holds the credentials and runs `repo maintenance --mode complete --force=true` with the same `--hostname corso-backup-server --user 1000:1000` as backups. Its exit code and the last lines of its output go into the report.
- `--force=true` rewrites the maintenance owner to the current `user@host` and runs regardless of ownership. It does not lower the 24-hour safety margin. It also removes protection against two maintenance runs overlapping on one repo, so:
  - the job's lock must be held for the whole run;
  - the maintenance script itself takes no lock, so don't run it by hand while the job might be running (around 06:30 to 07:30 on `maintenance_day`).
- Space is reclaimed with a delay. A pass reclaims snapshots deleted more than about 24h earlier, and may first grow the repo while it rewrites data (April: 143 → 174 → 63 GB over two passes a day apart). Daily deletes plus a weekly pass handle this without extra logic.
- The report records repo size (`du -sb /mnt/corso/repo`) and free space on `/mnt/corso` before and after each pass.

## Running

- Script: `prune_job.py` in the corso-tui container (it already has the Docker socket and the stub). It reuses `corso.py` for listing and deleting.
- Snapshot listing: `backup list <service> --json`, falling back to the existing fixed-width parser if the JSON lacks a field the job needs. Verified on the first dry run.
- Deletion: `backup delete <service> --backups id1,id2,…` in batches of `delete_batch_size`.
- Trigger: `m365backup.sh` calls the job at the end of the 06:30 run, so it never overlaps a backup and finishes well before the 2 AM rsync:
  `cd /home/user/corso-tui && docker compose run --rm -T --entrypoint python corso-tui /app/prune_job.py >> /mnt/corso_tui/prune_cron.log 2>&1`
  (`-T` because there is no terminal.)
- Order per run: lock → list snapshots (all services) → plan → guards → delete → maintenance (if due) → report.
- Flags: `--dry-run`, `--no-cap`, `--no-email`, `--resource NAME` (one user or site), `--maintenance`, `--no-maintenance`.

## Report

- Written to `/mnt/corso_tui/prune_reports/YYYY-MM-DD_HHMMSS[-dry-run].txt` and `/mnt/corso_tui/last_prune.txt`. The full list of deleted IDs goes to `<same name>.deletes.txt`.
- Subject: `[corso] FAIL 0 / WARN 0 / SKIPPED 0 / OK 6`.
- Summary first: "Needs attention" (every FAIL/WARN/SKIPPED reason) or "Everything OK.", plus the maintenance result if a pass ran.
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
```

## Rollout

1. Check free space for the maintenance swell (about 72 GB plus 25%) on the NAS, the backup server and the mirror. The 2 AM rsync between the two passes copies the swollen repo.
2. `--dry-run --no-maintenance`, then review the report.
3. One run with `--no-cap --no-maintenance` to clear the backlog.
4. `--maintenance` (pass 1; repo may grow).
5. More than 24h later, `--maintenance` again (pass 2; space is reclaimed). Compare with the 76.6 GB before; expect at least about 9 GB less.
6. Add the call to the end of the 06:30 `m365backup.sh` run.

## Deployment

- Add `prune_job.py` to the `COPY` line in `Dockerfile`.
- Add `tzdata` to `requirements.txt` (the slim image may lack zone data).
- Copy changed files to the NAS `~/corso-tui`, then `docker compose build`.
