# Development Log

## 2026-04-27 — Initial Build Session

### Context
Daniel uses the Corso backup agent to back up Microsoft 365 (Exchange, OneDrive, SharePoint) for two users. Backups run 4x daily via crontab, producing thousands of snapshots since July 2024. He needed a tool to browse, export, restore, and delete snapshots without memorizing corso CLI syntax.

### Design Decisions

**TUI over web UI** — No web server overhead on the Raspberry Pi NAS. Textual gives rich interactive widgets (tables, trees, dialogs) in a terminal.

**Docker container** — Consistent with Daniel's all-Docker infrastructure. The TUI container mounts the Docker socket to spawn sibling corso containers via the existing bash wrapper scripts.

**Credentials isolation** — The TUI never sees M365 credentials. Bash scripts handle auth and are never read by the tool. Scripts live in `/mnt/corso_tui/scripts/` and export sensitive env vars before calling `docker run` with the corso image.

**Sibling containers** — The bash scripts call `docker run` which talks to the host Docker daemon via the mounted socket. The corso container mounts host paths directly (`/mnt/corso:/app/corso`), independent of the TUI container's mounts. The TUI only needs `/mnt/corso` mounted read-only so the Docker CLI can read `corso.env` for `--env-file`.

### Key Technical Challenges

**Fixed-width parsing** — Corso outputs fixed-width columnar data. Exchange details contain three sections (Calendar, Contacts, Emails) with different headers in a single output. The parser detects header lines and switches column layouts mid-stream.

**Multi-word column names** — Headers like "Started at", "Protected resource", "Contact Name" use single spaces internally but 2+ spaces between columns. The parser handles this by scanning for column boundaries.

**Argument passing** — Initially passed all corso args as a single quoted string (`$1`). Spaces in folder names and paths caused quoting nightmares. Resolved by switching scripts to `"$@"` and passing args as a proper list via `subprocess.run`.

**Service-specific flags** — Each service uses different flags:
- Exchange: `--email`, `--email-folder` (not `--file`/`--folder`)
- OneDrive: `--file`, `--folder`
- SharePoint: `--file`, `--library`, `--folder` (library split from path)
- No service accepts `--user` for export/restore

**Async UI updates** — Blocking corso calls (10+ seconds for large snapshot lists) froze the UI. Solved with `asyncio.to_thread()` to run blocking calls in a thread pool while keeping the Textual event loop responsive for status bar updates.

### What Works
- List/filter snapshots by user, service, date range
- Sort newest-first or oldest-first
- Browse snapshot contents with folder tree (Exchange: emails/calendar/contacts, OneDrive: file tree, SharePoint: library/folder tree)
- Export individual items or folders for all three services
- Restore items or folders with destination and collision options
- Bulk delete with batching (≤100 per call) and confirmation
- Status indicators during long operations
- Error output saved to `/mnt/corso_tui/last_output.log`

### Future Ideas
- Detail caching — save corso detail output to file, load from cache on repeat browse
- Pattern search within snapshot contents
- Pre-populate cache via cron
- Snapshot comparison between dates
