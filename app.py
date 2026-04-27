"""Corso Backup Manager TUI."""
import asyncio
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable, Footer, Header, Label, Select, Input, Button, Tree, Static, TextArea,
)

import corso

CONFIG_PATH = Path("/mnt/corso_tui/config.yaml")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ── Confirmation dialog ──────────────────────────────────────────────────────

class ConfirmScreen(ModalScreen[bool]):
    """Yes/No confirmation dialog."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self.message, id="confirm-msg")
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", variant="error", id="yes")
                yield Button("No", variant="primary", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


# ── Output screen ────────────────────────────────────────────────────────────

class OutputScreen(ModalScreen):
    """Shows command output."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self.title_text = title
        self.output_text = text
        # Write to log file for easy access outside TUI
        self.log_path = Path("/mnt/corso_tui/last_output.log")
        self.log_path.write_text(f"{title}\n{'=' * len(title)}\n{text}\n")

    def compose(self) -> ComposeResult:
        with Vertical(id="output-dialog"):
            yield Label(self.title_text, id="output-title")
            yield TextArea(self.output_text, read_only=True, id="output-body")
            yield Label(f"Saved to {self.log_path}", id="output-path")
            yield Button("Close", id="close-output")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def action_dismiss(self) -> None:
        self.app.pop_screen()


# ── Restore/Export options screen ─────────────────────────────────────────────

class RestoreExportScreen(ModalScreen):
    """Prompt for restore/export options."""

    def __init__(self, action: str, service: str, backup_id: str, item, item_type: str, user_email: str) -> None:
        super().__init__()
        self.action = action  # "restore" or "export"
        self.service = service
        self.backup_id = backup_id
        self.item = item  # dict for leaf items, str for folder nodes
        self.item_type = item_type  # "file" or "folder"
        self.user_email = user_email

    def _display_name(self) -> str:
        if isinstance(self.item, dict):
            return self.item.get("ItemName", self.item.get("Subject", self.item.get("Contact Name", str(self.item.get("ID", "")))))
        return self.item

    def compose(self) -> ComposeResult:
        with Vertical(id="re-dialog"):
            yield Label(f"{self.action.title()}: {self._display_name()}", id="re-title")
            if self.action == "restore":
                yield Label("Destination (empty=new Corso folder, /=original path):")
                yield Input(placeholder="e.g. Recovery_Folder", id="destination")
                yield Label("Collision policy:")
                yield Select(
                    [("copy (default)", "copy"), ("replace", "replace"), ("skip", "skip")],
                    value="copy", id="collisions",
                )
            with Horizontal(id="re-buttons"):
                yield Button(f"Run {self.action.title()}", variant="warning", id="run")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss()
            return

        cfg = load_config()
        kwargs = {}

        if self.action == "restore":
            if self.service == "exchange":
                if isinstance(self.item, dict):
                    kwargs["email"] = self.item.get("ID", "")
                else:
                    kwargs["email_folder"] = self.item
            elif self.service == "onedrive":
                if isinstance(self.item, dict):
                    kwargs["file"] = self.item.get("ID", "")
                else:
                    kwargs["folder"] = self.item
            elif self.service == "sharepoint":
                if isinstance(self.item, dict):
                    kwargs["file"] = self.item.get("ID", "")
                else:
                    parts = self.item.split("/", 1)
                    kwargs["library"] = parts[0]
                    if len(parts) > 1:
                        kwargs["folder"] = parts[1]
            dest = self.query_one("#destination", Input).value.strip()
            coll = self.query_one("#collisions", Select).value
            if dest:
                kwargs["destination"] = dest
            if coll != "copy":
                kwargs["collisions"] = coll
            output = corso.restore(cfg["scripts"]["restore"], self.service, self.backup_id, **kwargs)
        else:
            # Export flags differ by service
            if self.service == "exchange":
                if isinstance(self.item, dict):
                    kwargs["email"] = self.item.get("ID", "")
                else:
                    kwargs["email_folder"] = self.item
            elif self.service == "onedrive":
                if isinstance(self.item, dict):
                    kwargs["file"] = self.item.get("ID", "")
                else:
                    kwargs["folder"] = self.item
            elif self.service == "sharepoint":
                if isinstance(self.item, dict):
                    kwargs["file"] = self.item.get("ID", "")
                    lib = self.item.get("Library", "")
                    if lib:
                        kwargs["library"] = lib
                    folder = self.item.get("ParentPath", "")
                    if folder:
                        kwargs["folder"] = folder
                else:
                    # Folder node — path is "Library/subfolder/..."
                    parts = self.item.split("/", 1)
                    kwargs["library"] = parts[0]
                    if len(parts) > 1:
                        kwargs["folder"] = parts[1]
            export_dir = cfg.get("export_dir", "/mnt/corso_tui/exports")
            if self.service == "onedrive":
                export_dir += "/OneDrive"
            output = corso.export(cfg["scripts"]["export"], self.service, self.backup_id, export_dir=export_dir, **kwargs)

        self.dismiss()
        self.app.push_screen(OutputScreen(f"{self.action.title()} Result", output or "Command completed."))


# ── Detail browser screen ────────────────────────────────────────────────────

class DetailScreen(ModalScreen):
    """Browse snapshot contents and restore/export."""

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("r", "restore_item", "Restore"),
        Binding("e", "export_item", "Export"),
    ]

    def __init__(self, service: str, backup_id: str, user_email: str) -> None:
        super().__init__()
        self.service = service
        self.backup_id = backup_id
        self.user_email = user_email
        self.items: list[dict[str, str]] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-screen"):
            yield Label(f"Snapshot: {self.backup_id} | \\[r]estore  \\[e]xport  \\[esc]back", id="detail-header")
            yield Label("⏳ Loading snapshot contents...", id="detail-status")
            yield Tree("Contents", id="detail-tree")

    def on_mount(self) -> None:
        self.loading = True
        self.run_worker(self._load_details())

    async def _load_details(self) -> None:
        cfg = load_config()
        self.items = await asyncio.to_thread(corso.get_details, cfg["scripts"]["query"], self.service, self.backup_id)
        tree = self.query_one("#detail-tree", Tree)
        tree.clear()
        tree.root.expand()
        self._build_tree(tree)
        self.query_one("#detail-status", Label).update(f"✓ {len(self.items)} items loaded")
        self.loading = False

    def _build_tree(self, tree: Tree) -> None:
        """Build a folder tree from detail items."""
        folders: dict = {}

        if self.service == "exchange":
            # Group by type based on which keys are present
            cal_node = None
            contacts_node = None
            email_folders: dict = {}

            for item in self.items:
                if "Organizer" in item:
                    # Calendar event
                    if cal_node is None:
                        cal_node = tree.root.add("📅 Calendar")
                    subj = item.get("Subject", "(no subject)")
                    org = item.get("Organizer", "")
                    starts = item.get("Starts", "")
                    recur = item.get("Recurring", "")
                    label = f"📅 {subj[:70]}  \\[{org}] {starts}"
                    if recur and recur.lower() not in ("", "false"):
                        label += " 🔁"
                    node = cal_node.add_leaf(label)
                    node.data = item
                elif "Contact Name" in item:
                    # Contact
                    if contacts_node is None:
                        contacts_node = tree.root.add("👤 Contacts")
                    name = item.get("Contact Name", "(unknown)")
                    node = contacts_node.add_leaf(f"👤 {name}")
                    node.data = item
                else:
                    # Email
                    folder = item.get("Folder", "Unknown")
                    if folder not in email_folders:
                        fnode = tree.root.add(f"📁 {folder}")
                        fnode.data = folder
                        email_folders[folder] = fnode
                    subject = item.get("Subject", "")
                    sender = item.get("Sender", "")
                    received = item.get("Received", "")
                    label = f"✉ {subject[:80]}  \\[{sender}] {received}"
                    node = email_folders[folder].add_leaf(label)
                    node.data = item
        elif self.service == "onedrive":
            for item in self.items:
                path = item.get("ParentPath", "")
                name = item.get("ItemName", "")
                size = item.get("Size", "")
                parts = [p for p in path.split("/") if p]
                current = tree.root
                built = ""
                for part in parts:
                    built = f"{built}/{part}" if built else part
                    found = None
                    for child in current.children:
                        if child.data and isinstance(child.data, str) and child.data == built:
                            found = child
                            break
                    if found:
                        current = found
                    else:
                        node = current.add(f"📁 {part}")
                        node.data = built
                        current = node
                label = f"📄 {name}  ({size})"
                leaf = current.add_leaf(label)
                leaf.data = item
        elif self.service == "sharepoint":
            for item in self.items:
                lib = item.get("Library", "")
                path = item.get("ParentPath", "")
                name = item.get("ItemName", "")
                size = item.get("Size", "")
                full = f"{lib}/{path}" if path else lib
                parts = [p for p in full.split("/") if p]
                current = tree.root
                built = ""
                for part in parts:
                    built = f"{built}/{part}" if built else part
                    found = None
                    for child in current.children:
                        if child.data and isinstance(child.data, str) and child.data == built:
                            found = child
                            break
                    if found:
                        current = found
                    else:
                        node = current.add(f"📁 {part}")
                        node.data = built
                        current = node
                label = f"📄 {name}  ({size})"
                leaf = current.add_leaf(label)
                leaf.data = item

    def _get_selected_item(self) -> tuple | None:
        """Return selection info for the selected tree node.
        For leaf items: (item_dict, 'file')
        For folder nodes: (folder_path_str, 'folder')
        """
        tree = self.query_one("#detail-tree", Tree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return None

        if isinstance(node.data, dict):
            return node.data, "file"
        elif isinstance(node.data, str):
            return node.data, "folder"
        return None

    def action_restore_item(self) -> None:
        sel = self._get_selected_item()
        if sel:
            item, item_type = sel
            self.app.push_screen(RestoreExportScreen("restore", self.service, self.backup_id, item, item_type, self.user_email))

    def action_export_item(self) -> None:
        sel = self._get_selected_item()
        if sel:
            item, item_type = sel
            self.app.push_screen(RestoreExportScreen("export", self.service, self.backup_id, item, item_type, self.user_email))

    def action_go_back(self) -> None:
        self.app.pop_screen()


# ── Main app ──────────────────────────────────────────────────────────────────

class CorsoApp(App):
    """Corso Backup Manager."""

    CSS = """
    #top-bar { height: 3; padding: 0 1; }
    #top-bar Select { width: 20; }
    #top-bar Input { width: 16; }
    #top-bar Button { margin-left: 1; }
    #snapshot-table { height: 1fr; }
    #status-bar { height: 1; padding: 0 1; }
    #confirm-dialog { width: 60; height: 12; border: thick $accent; padding: 1 2; align: center middle; }
    #confirm-buttons { height: 3; align: center middle; }
    #output-dialog { width: 80%; height: 80%; border: thick $accent; padding: 1 2; }
    #output-body { height: 1fr; }
    #re-dialog { width: 70; height: 18; border: thick $accent; padding: 1 2; align: center middle; }
    #re-buttons { height: 3; align: center middle; }
    #detail-screen { width: 100%; height: 100%; }
    #detail-tree { height: 1fr; }
    .marked { background: $warning 30%; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("l", "load_snapshots", "Load"),
        Binding("m", "toggle_mark", "Mark/Unmark"),
        Binding("d", "delete_marked", "Delete Marked"),
        Binding("b", "browse_snapshot", "Browse"),
        Binding("a", "mark_all_visible", "Mark All"),
        Binding("u", "unmark_all", "Unmark All"),
        Binding("s", "toggle_sort", "Sort ↑↓"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_config()
        self.snapshots: list[corso.Snapshot] = []
        self.marked: set[str] = set()
        self.sort_newest_first = True

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top-bar"):
            yield Select(
                [(name, name) for name in self.cfg["users"]],
                prompt="User", id="user-select",
            )
            yield Select(
                [("exchange", "exchange"), ("onedrive", "onedrive"), ("sharepoint", "sharepoint")],
                prompt="Service", id="service-select",
            )
            yield Input(placeholder="From YYYY-MM-DD", id="date-from")
            yield Input(placeholder="To YYYY-MM-DD", id="date-to")
            yield Button("Load", id="load-btn")
        yield DataTable(id="snapshot-table")
        yield Label("Ready. Select user + service, set date range, press Load.", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#snapshot-table", DataTable)
        table.cursor_type = "row"
        table.add_column("✓", key="mark", width=3)
        table.add_column("ID", key="id")
        table.add_column("Started", key="started")
        table.add_column("Duration", key="duration")
        table.add_column("Status", key="status")
        table.add_column("User", key="user")
        table.add_column("Data", key="data")
        # Default date range: last 30 days
        now = datetime.now(timezone.utc)
        self.query_one("#date-from", Input).value = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        self.query_one("#date-to", Input).value = now.strftime("%Y-%m-%d")

    def _status(self, msg: str) -> None:
        self.query_one("#status-bar", Label).update(msg)

    def action_load_snapshots(self) -> None:
        self._do_load()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "load-btn":
            self._do_load()

    def _do_load(self) -> None:
        user_sel = self.query_one("#user-select", Select).value
        svc_sel = self.query_one("#service-select", Select).value
        if user_sel is Select.BLANK or svc_sel is Select.BLANK:
            self._status("Select both user and service first.")
            return

        user_email = self.cfg["users"][user_sel]
        date_from_str = self.query_one("#date-from", Input).value.strip()
        date_to_str = self.query_one("#date-to", Input).value.strip()

        self._status(f"⏳ Querying {svc_sel} snapshots for {user_sel}... (this may take a moment)")
        self.marked.clear()
        table = self.query_one("#snapshot-table", DataTable)
        table.clear()
        filter_email = user_email if svc_sel != "sharepoint" else None
        self.run_worker(self._load(svc_sel, filter_email, date_from_str, date_to_str))

    async def _load(self, service: str, user_email: str, date_from: str, date_to: str) -> None:
        try:
            all_snaps = await asyncio.to_thread(corso.list_snapshots, self.cfg["scripts"]["query"], service, user_email)
        except Exception as e:
            self._status(f"Error: {e}")
            return

        self._status(f"⏳ Filtering {len(all_snaps)} snapshots by date range...")

        # Filter by date range
        try:
            d_from = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc) if date_from else None
        except ValueError:
            d_from = None
        try:
            d_to = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc, hour=23, minute=59, second=59) if date_to else None
        except ValueError:
            d_to = None

        filtered = []
        for s in all_snaps:
            if d_from and s.started < d_from:
                continue
            if d_to and s.started > d_to:
                continue
            filtered.append(s)

        self.snapshots = sorted(filtered, key=lambda s: s.started, reverse=self.sort_newest_first)

        self._populate_table(service)
        self._status(f"Loaded {len(self.snapshots)} snapshots (filtered from {len(all_snaps)} total).")

    def _populate_table(self, service: str) -> None:
        table = self.query_one("#snapshot-table", DataTable)
        table.clear(columns=True)
        col_label = "Path" if service == "sharepoint" else "User"
        table.add_column("✓", key="mark", width=3)
        table.add_column("ID", key="id")
        table.add_column("Started", key="started")
        table.add_column("Duration", key="duration")
        table.add_column("Status", key="status")
        table.add_column(col_label, key="user")
        table.add_column("Data", key="data")
        for s in self.snapshots:
            mark = "✓" if s.id in self.marked else ""
            table.add_row(
                mark, s.id[:12], s.started.strftime("%Y-%m-%d %H:%M"),
                s.duration, s.status, s.user, s.data,
                key=s.id,
            )

    def action_toggle_mark(self) -> None:
        table = self.query_one("#snapshot-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        sid = row_key.value
        if sid in self.marked:
            self.marked.discard(sid)
            table.update_cell(row_key, "mark", "")
        else:
            self.marked.add(sid)
            table.update_cell(row_key, "mark", "✓")
        # Move cursor down
        if table.cursor_coordinate.row < table.row_count - 1:
            table.move_cursor(row=table.cursor_coordinate.row + 1)
        self._status(f"{len(self.marked)} snapshots marked for deletion.")

    def action_mark_all_visible(self) -> None:
        table = self.query_one("#snapshot-table", DataTable)
        for s in self.snapshots:
            self.marked.add(s.id)
        for row_key in table.rows:
            table.update_cell(row_key, "mark", "✓")
        self._status(f"{len(self.marked)} snapshots marked.")

    def action_unmark_all(self) -> None:
        table = self.query_one("#snapshot-table", DataTable)
        self.marked.clear()
        for row_key in table.rows:
            table.update_cell(row_key, "mark", "")
        self._status("All marks cleared.")

    def action_toggle_sort(self) -> None:
        self.sort_newest_first = not self.sort_newest_first
        if self.snapshots:
            self.snapshots.reverse()
            svc = self.query_one("#service-select", Select).value
            if svc is not Select.BLANK:
                self._populate_table(svc)
        direction = "newest first" if self.sort_newest_first else "oldest first"
        self._status(f"Sort: {direction}")

    def action_delete_marked(self) -> None:
        if not self.marked:
            self._status("No snapshots marked for deletion.")
            return

        svc_sel = self.query_one("#service-select", Select).value
        if svc_sel is Select.BLANK:
            self._status("No service selected.")
            return

        count = len(self.marked)
        self.push_screen(
            ConfirmScreen(f"Delete {count} {svc_sel} snapshot(s)?\n\nThis cannot be undone."),
            callback=self._on_delete_confirm,
        )

    def _on_delete_confirm(self, confirmed: bool) -> None:
        if not confirmed:
            self._status("Deletion cancelled.")
            return
        svc_sel = self.query_one("#service-select", Select).value
        self.run_worker(self._do_delete(svc_sel))

    async def _do_delete(self, service: str) -> None:
        ids = list(self.marked)
        batch_size = self.cfg.get("delete_batch_size", 100)
        total = len(ids)
        deleted = 0
        outputs = []

        for i in range(0, total, batch_size):
            batch = ids[i:i + batch_size]
            self._status(f"⏳ Deleting batch {i // batch_size + 1} ({len(batch)} snapshots)...")
            output = await asyncio.to_thread(corso.delete_snapshots, self.cfg["scripts"]["query"], service, batch)
            outputs.append(output)
            deleted += len(batch)

        self.marked.clear()
        self._status(f"Deleted {deleted} snapshots. Reload to refresh.")
        self.push_screen(OutputScreen("Delete Results", "\n".join(o for o in outputs if o.strip()) or f"Deleted {deleted} snapshots."))

    def action_browse_snapshot(self) -> None:
        table = self.query_one("#snapshot-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        sid = row_key.value

        svc_sel = self.query_one("#service-select", Select).value
        user_sel = self.query_one("#user-select", Select).value
        if svc_sel is Select.BLANK or user_sel is Select.BLANK:
            return

        user_email = self.cfg["users"][user_sel]
        self.push_screen(DetailScreen(svc_sel, sid, user_email))


def main():
    app = CorsoApp()
    app.run()


if __name__ == "__main__":
    main()
