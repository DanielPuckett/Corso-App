"""Wrapper around corso CLI via the m365*.sh scripts."""
import subprocess
import re
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Snapshot:
    id: str
    started: datetime
    duration: str
    status: str
    user: str
    data: str


@dataclass
class ExchangeItem:
    id: str
    sender: str
    folder: str
    subject: str
    received: str


@dataclass
class OneDriveItem:
    id: str
    name: str
    parent_path: str
    size: str
    owner: str
    created: str
    modified: str


@dataclass
class SharePointItem:
    id: str
    name: str
    library: str
    parent_path: str
    size: str
    owner: str
    created: str
    modified: str


def _run(script: str, args: list[str]) -> str:
    """Run a wrapper script with args passed as separate arguments."""
    cmd = ["bash", script] + args
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0 and result.stderr:
        return f"ERROR: {result.stderr}\n{result.stdout}\n\nCOMMAND: {cmd}"
    return result.stdout


def _parse_header(header: str) -> list[tuple[str, int]]:
    """Extract column names and start positions from a header line."""
    cols = []
    i = 0
    length = len(header)
    while i < length:
        # Skip leading whitespace
        while i < length and header[i] == " ":
            i += 1
        if i >= length:
            break
        # Start of a column name
        start = i
        # Advance to end of column name: stop when we hit 2+ spaces or end of line
        while i < length:
            if header[i] == " ":
                # Count consecutive spaces
                j = i
                while j < length and header[j] == " ":
                    j += 1
                if j - i >= 2 or j >= length:
                    # Column boundary
                    break
                else:
                    # Single space within column name (e.g. "Started at")
                    i = j
            else:
                i += 1
        name = header[start:i].strip()
        if name:
            cols.append((name, start))
    return cols


def _parse_fixed_width(output: str) -> list[dict[str, str]]:
    """Parse fixed-width column output from corso into list of dicts.
    Handles multiple sections with different headers (e.g. exchange details)."""
    lines = output.strip().split("\n")
    if not lines:
        return []

    rows = []
    cols = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Detect header lines: starts with "ID" followed by spaces then another word
        if stripped.startswith("ID") and len(stripped) > 4 and stripped[2] == " ":
            cols = _parse_header(line)
            continue
        if not cols:
            continue
        row = {}
        for idx, (name, start) in enumerate(cols):
            if idx + 1 < len(cols):
                end = cols[idx + 1][1]
            else:
                end = len(line)
            val = line[start:end].strip() if start < len(line) else ""
            row[name] = val
        # Skip rows where ID is empty (malformed)
        if row.get("ID"):
            rows.append(row)
    return rows


def list_snapshots(query_script: str, service: str, user_email: str | None = None) -> list[Snapshot]:
    """List snapshots for a service, optionally filtered by user."""
    args = ["list", service]
    output = _run(query_script, args)
    rows = _parse_fixed_width(output)
    snapshots = []
    for r in rows:
        try:
            started = datetime.fromisoformat(r.get("Started at", "").replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        snap = Snapshot(
            id=r.get("ID", ""),
            started=started,
            duration=r.get("Duration", ""),
            status=r.get("Status", ""),
            user=r.get("Protected resource", ""),
            data=r.get("Data", ""),
        )
        if user_email and snap.user != user_email:
            continue
        snapshots.append(snap)
    return snapshots


def get_details(query_script: str, service: str, backup_id: str) -> list[dict[str, str]]:
    """Get detail rows for a specific backup."""
    args = ["details", service, "--backup", backup_id]
    output = _run(query_script, args)
    return _parse_fixed_width(output)


def delete_snapshots(query_script: str, service: str, ids: list[str]) -> str:
    """Delete snapshots by ID. Returns output."""
    id_str = ",".join(ids)
    args = ["delete", service, "--backups", id_str]
    return _run(query_script, args)


def restore(restore_script: str, service: str, backup_id: str, **kwargs) -> str:
    """Run a restore command. kwargs become --flag value pairs."""
    args = [service, "--backup", backup_id]
    for k, v in kwargs.items():
        flag = k.replace("_", "-")
        args.extend([f"--{flag}", v])
    return _run(restore_script, args)


def export(export_script: str, service: str, backup_id: str, export_dir: str = "/mnt/corso_tui/exports", **kwargs) -> str:
    """Run an export command. kwargs become --flag value pairs."""
    args = [service, "--backup", backup_id]
    for k, v in kwargs.items():
        flag = k.replace("_", "-")
        args.extend([f"--{flag}", v])
    args.append(export_dir)
    return _run(export_script, args)
