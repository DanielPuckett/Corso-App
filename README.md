# Corso TUI

A terminal UI for managing [Corso](https://corsobackup.io/) Microsoft 365 backup snapshots. Built with Python/Textual, runs in Docker.

## What it does

- **List snapshots** for Exchange, OneDrive, and SharePoint with date range filtering
- **Browse snapshot contents** — emails by folder, calendar events, contacts, OneDrive files, SharePoint libraries
- **Export** individual items or entire folders to local disk
- **Restore** items or folders back to Microsoft 365 (in-place or to a new location)
- **Delete** old snapshots in bulk with confirmation
- **Discover users** automatically from existing snapshots

## Architecture

```
┌──────────────────────────────────────────────┐
│  corso-tui container                         │
│  Python/Textual TUI                          │
│  ├── Reads config from /mnt/corso_tui/       │
│  ├── Calls bash stub script via subprocess   │
│  └── Script spawns corso containers via      │
│      Docker socket (sibling containers)      │
└──────────────────────────────────────────────┘
        │                          │
   Docker socket            /mnt/corso_tui
        │                   (config, scripts,
        ▼                    exports)
┌──────────────┐
│ corso container (ghcr.io/alcionai/corso)     │
│ Talks to M365 APIs and local repo            │
└──────────────┘
```

The TUI container never touches credentials directly. A bash wrapper script handles authentication and spawns the corso container with the correct environment.

## Requirements

- Docker + Docker Compose
- Corso backup agent already configured with M365 credentials
- A bash wrapper script (`m365stub.sh`) that accepts corso subcommand arguments via `"$@"` (see `m365stub.sample.sh`)

## File Layout

```
~/corso-tui/                   # Build files
  ├── Dockerfile
  ├── docker-compose.yaml
  ├── app.py                   # TUI application
  ├── corso.py                 # Corso CLI wrapper
  └── requirements.txt

/mnt/corso_tui/                # Runtime data (mounted rw)
  ├── config.yaml              # User config (copy from config.sample.yaml)
  ├── scripts/                 # Bash wrapper scripts
  │   └── m365stub.sh          # Credential wrapper (copy from m365stub.sample.sh)
  ├── exports/                 # Exported files land here
  └── last_output.log          # Last command output for debugging
```

## Docker Mounts

| Host | Container | Mode | Purpose |
|------|-----------|------|---------|
| `/mnt/corso_tui` | `/mnt/corso_tui` | rw | Config, scripts, exports |
| `/mnt/corso` | `/mnt/corso` | ro | Corso env file for Docker CLI |
| `/var/run/docker.sock` | `/var/run/docker.sock` | rw | Spawn sibling corso containers |

## Configuration

Copy `config.sample.yaml` to `/mnt/corso_tui/config.yaml` and edit:

```yaml
users:
  alice: alice@contoso.com
  bob: bob@contoso.com

scripts:
  stub: /mnt/corso_tui/scripts/m365stub.sh

export_dir: /mnt/corso_tui/exports
delete_batch_size: 100
```

Users can also be auto-discovered by pressing F5 in the app.

## Usage

```bash
cd ~/corso-tui
docker compose build
docker compose run --rm corso-tui
```

## Keybindings

### Main screen

| Key | Action |
|-----|--------|
| `l` | Load snapshots |
| `s` | Toggle sort (newest/oldest first) |
| `m` | Mark/unmark snapshot (advances cursor) |
| `a` | Mark all visible |
| `u` | Unmark all |
| `d` | Delete marked (with confirmation) |
| `b` | Browse snapshot contents |
| `F5` | Refresh user list from snapshots |
| `q` | Quit |

### Browse screen

| Key | Action |
|-----|--------|
| `r` | Restore selected item/folder |
| `e` | Export selected item/folder |
| `Esc` | Back to snapshot list |

## Wrapper Script

The stub script receives the full corso command as separate arguments via `"$@"`. Examples of what the TUI passes:

```bash
# List snapshots
m365stub.sh backup list exchange

# Snapshot details
m365stub.sh backup details onedrive --backup <ID>

# Delete snapshots
m365stub.sh backup delete exchange --backups <ID1>,<ID2>

# Export
m365stub.sh export onedrive --backup <ID> --file <ITEM_ID> /mnt/corso_tui/exports/alice@contoso.com/OneDrive
m365stub.sh export exchange --backup <ID> --email-folder Inbox /mnt/corso_tui/exports/alice@contoso.com
m365stub.sh export sharepoint --backup <ID> --library Documents --folder "subfolder" /mnt/corso_tui/exports/contoso.sharepoint.com/sites/TeamSite

# Restore
m365stub.sh restore onedrive --backup <ID> --file <ITEM_ID> --destination /
m365stub.sh restore exchange --backup <ID> --email <ITEM_ID>
m365stub.sh restore sharepoint --backup <ID> --library Documents --folder "subfolder"
```

See `m365stub.sample.sh` for a template.

## Scheduled Prune

`prune_job.py` checks that every user × service got a fresh, successful snapshot, thins old snapshots (keep all for 7 days, daily to 3 months, monthly after), runs weekly repository maintenance through `scripts.maint`, and emails a report. Full design: [PRUNE_SPEC.md](PRUNE_SPEC.md). Config: the `prune:` section of `config.yaml` (see `config.sample.yaml`). SMTP settings: `/mnt/corso_tui/.env` (see `.env.sample`). Maintenance script: see `m365maint.sample.sh`.

```bash
# Preview, change nothing
docker compose run --rm --entrypoint python corso-tui /app/prune_job.py --dry-run --no-maintenance
# One-time backlog cleanup, then a maintenance pass
docker compose run --rm --entrypoint python corso-tui /app/prune_job.py --no-cap --no-maintenance
docker compose run --rm --entrypoint python corso-tui /app/prune_job.py --maintenance
```

Called at the end of the 06:30 backup run (`-T` because there is no terminal):

```
cd /home/user/corso-tui && docker compose run --rm -T --entrypoint python corso-tui /app/prune_job.py >> /mnt/corso_tui/prune_cron.log 2>&1
```

Reports go to `/mnt/corso_tui/prune_reports/`. Tests (no Corso needed): `python3 tests/test_prune_plan.py`.

Note: pressing F5 in the TUI rewrites `config.yaml` and drops its comments; the values, including `prune:`, are kept.

## Platform

- Raspberry Pi (aarch64), Debian
- Docker + Docker Compose
- Python 3.12, Textual 1.0.0
