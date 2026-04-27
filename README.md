# Corso TUI

A terminal UI for managing [Corso](https://corsobackup.io/) Microsoft 365 backup snapshots. Built with Python/Textual, runs in Docker.

## What it does

- **List snapshots** for Exchange, OneDrive, and SharePoint with date range filtering
- **Browse snapshot contents** — emails by folder, calendar events, contacts, OneDrive files, SharePoint libraries
- **Export** individual items or entire folders to local disk
- **Restore** items or folders back to Microsoft 365 (in-place or to a new location)
- **Delete** old snapshots in bulk with confirmation

## Architecture

```
┌──────────────────────────────────────────────┐
│  corso-tui container                         │
│  Python/Textual TUI                          │
│  ├── Reads config from /mnt/corso_tui/       │
│  ├── Calls bash scripts via subprocess       │
│  └── Scripts spawn corso containers via      │
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

The TUI container never touches credentials directly. Bash wrapper scripts handle authentication and spawn the corso container with the correct environment.

## Requirements

- Docker + Docker Compose
- Corso backup agent already configured with M365 credentials
- Bash wrapper scripts (`m365query.sh`, `m365export.sh`, `m365restore.sh`) that accept corso subcommand arguments via `"$@"`

## File Layout

```
/home/user/corso-tui/       # Build files
  ├── Dockerfile
  ├── docker-compose.yaml
  ├── app.py                   # TUI application
  ├── corso.py                 # Corso CLI wrapper
  └── requirements.txt

/mnt/corso_tui/                # Runtime data (mounted rw)
  ├── config.yaml              # User config
  ├── scripts/                 # Bash wrapper scripts
  │   ├── m365query.sh
  │   ├── m365export.sh
  │   └── m365restore.sh
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

`/mnt/corso_tui/config.yaml`:

```yaml
users:
  daniel: alice@contoso.com
  jacqui: bob@contoso.com

scripts:
  query: /mnt/corso_tui/scripts/m365query.sh
  restore: /mnt/corso_tui/scripts/m365restore.sh
  export: /mnt/corso_tui/scripts/m365export.sh

export_dir: /mnt/corso_tui/exports
delete_batch_size: 100
```

## Usage

```bash
cd /home/user/corso-tui
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
| `q` | Quit |

### Browse screen

| Key | Action |
|-----|--------|
| `r` | Restore selected item/folder |
| `e` | Export selected item/folder |
| `Esc` | Back to snapshot list |

## Wrapper Scripts

Scripts must accept corso subcommand arguments as separate positional parameters (`"$@"`). Examples of what the TUI passes:

```bash
# List snapshots
m365query.sh list exchange

# Snapshot details
m365query.sh details onedrive --backup <ID>

# Delete snapshots
m365query.sh delete exchange --backups <ID1>,<ID2>

# Export
m365export.sh onedrive --backup <ID> --file <ITEM_ID> /mnt/corso_tui/exports/OneDrive
m365export.sh exchange --backup <ID> --email-folder Inbox /mnt/corso_tui/exports
m365export.sh sharepoint --backup <ID> --library Documents --folder "subfolder" /mnt/corso_tui/exports

# Restore
m365restore.sh onedrive --backup <ID> --file <ITEM_ID> --destination /
m365restore.sh exchange --backup <ID> --email <ITEM_ID>
m365restore.sh sharepoint --backup <ID> --library Documents --folder "subfolder"
```

## Platform

- Raspberry Pi (aarch64), Debian
- Docker 29.2.0, Compose v5.0.2
- Python 3.12, Textual 1.0.0
