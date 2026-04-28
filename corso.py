"""Wrapper around corso CLI via the m365stub.sh script."""
import subprocess
import re
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Snapshot:
    id: str
    started: datetime
    duration: str
    status: str
    user: str
    data: str


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
        while i < length and header[i] == " ":
            i += 1
        if i >= length:
            break
        start = i
        while i < length:
            if header[i] == " ":
                j = i
                while j < length and header[j] == " ":
                    j += 1
                if j - i >= 2 or j >= length:
                    break
                else:
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
        if row.get("ID"):
            rows.append(row)
    return rows


def list_snapshots(stub: str, service: str, user_email: str | None = None) -> list[Snapshot]:
    """List snapshots for a service, optionally filtered by user."""
    output = _run(stub, ["backup", "list", service])
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


def get_details(stub: str, service: str, backup_id: str) -> list[dict[str, str]]:
    """Get detail rows for a specific backup."""
    output = _run(stub, ["backup", "details", service, "--backup", backup_id])
    return _parse_fixed_width(output)


def delete_snapshots(stub: str, service: str, ids: list[str]) -> str:
    """Delete snapshots by ID. Returns output."""
    return _run(stub, ["backup", "delete", service, "--backups", ",".join(ids)])


def restore(stub: str, service: str, backup_id: str, **kwargs) -> str:
    """Run a restore command."""
    args = ["restore", service, "--backup", backup_id]
    for k, v in kwargs.items():
        args.extend([f"--{k.replace('_', '-')}", v])
    return _run(stub, args)


def export(stub: str, service: str, backup_id: str, export_dir: str = "/mnt/corso_tui/exports", **kwargs) -> str:
    """Run an export command."""
    args = ["export", service, "--backup", backup_id]
    for k, v in kwargs.items():
        args.extend([f"--{k.replace('_', '-')}", v])
    args.append(export_dir)
    return _run(stub, args)
