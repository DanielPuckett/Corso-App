"""Scheduled prune for the Corso repo: check freshness, thin snapshots, weekly maintenance, report.

See PRUNE_SPEC.md for the design. Run inside the container:
    python /app/prune_job.py [--dry-run] [--no-cap] [--no-email] [--resource NAME]
                             [--maintenance | --no-maintenance]
"""
from __future__ import annotations

import argparse
import calendar
import fcntl
import os
import re
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

import corso

CONFIG_PATH = "/mnt/corso_tui/config.yaml"
MAINT_TIMEOUT = 6 * 3600
SUCCESS = ("Completed", "No Data")

DEFAULT_PRUNE = {
    "report_dir": "/mnt/corso_tui/prune_reports",
    "last_report": "/mnt/corso_tui/last_prune.txt",
    "env_file": "/mnt/corso_tui/.env",
    "lock_file": "/mnt/corso_tui/prune.lock",
    "repo_path": "/mnt/corso/repo",
    "timezone": "America/Toronto",
    "services": ["exchange", "onedrive", "sharepoint"],
    "keep_last": 3,
    "max_delete_percent": 20,
    "fresh_within": "7h",
    "retired_resources": [],
    "keep_all_within": "7d",
    "tiers": [
        {"older_than": "7d", "keep": "daily"},
        {"older_than": "3M", "keep": "monthly"},
    ],
    "maintenance_day": "sunday",
}

FAIL, WARN, SKIPPED, OK = "FAIL", "WARN", "SKIPPED", "OK"
_RANK = {OK: 0, SKIPPED: 1, WARN: 2, FAIL: 3}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _worst(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


# ── Time helpers ─────────────────────────────────────────────────────────────

def _sub_months(dt: datetime, n: int) -> datetime:
    total = dt.year * 12 + (dt.month - 1) - n
    y, m = divmod(total, 12)
    m += 1
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


def ago(now: datetime, spec: str) -> datetime:
    """now minus a duration like '7h', '7d', '2w', '3M', '2y' (M/y are calendar-aware)."""
    m = re.fullmatch(r"\s*(\d+)\s*([hdwMy])\s*", str(spec))
    if not m:
        raise ValueError(f"bad duration {spec!r} (use e.g. 7h, 7d, 2w, 3M, 2y)")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "h":
        return now - timedelta(hours=n)
    if unit == "d":
        return now - timedelta(days=n)
    if unit == "w":
        return now - timedelta(weeks=n)
    if unit == "M":
        return _sub_months(now, n)
    return _sub_months(now, n * 12)


def bucket_key(t: datetime, kind: str) -> tuple:
    """Calendar bucket of a local time."""
    if kind == "daily":
        return (t.year, t.month, t.day)
    if kind == "weekly":
        iso = t.isocalendar()
        return (iso[0], iso[1])
    if kind == "monthly":
        return (t.year, t.month)
    if kind == "halfyearly":
        return (t.year, 0 if t.month <= 6 else 1)
    if kind == "yearly":
        return (t.year,)
    raise ValueError(f"unknown tier keep {kind!r} (use daily, weekly, monthly, halfyearly, yearly)")


def fmt_age(now: datetime, t: datetime) -> str:
    secs = (now - t).total_seconds()
    if secs < 3600 * 48:
        return f"{secs / 3600:.1f}h ago"
    return f"{secs / 86400:.0f}d ago"


def fmt_gb(n: int | None) -> str:
    return "?" if n is None else f"{n / 1e9:.2f} GB"


# ── Planning ─────────────────────────────────────────────────────────────────

def is_success(b) -> bool:
    return b.status in SUCCESS


@dataclass
class GroupPlan:
    resource: str
    service: str
    snaps: list  # oldest first
    keep: list = field(default_factory=list)
    reason: dict = field(default_factory=dict)  # kept id -> why: recent | daily | monthly | ... | floor | no tier
    delete: list = field(default_factory=list)
    fails: list = field(default_factory=list)
    warns: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def plan_group(snaps: list, cfg: dict, now: datetime, tz, retired: bool) -> GroupPlan:
    gs = sorted(snaps, key=lambda b: b.started)
    gp = GroupPlan(resource=gs[0].resource, service=gs[0].service, snaps=gs)
    ok_snaps = [b for b in gs if is_success(b)]
    local = lambda b: b.started.astimezone(tz)  # noqa: E731

    # Guards: freshness and status
    fresh_cut = ago(now, cfg.get("fresh_within", "7h"))
    newest_ok = ok_snaps[-1] if ok_snaps else None
    is_fresh = bool(newest_ok and newest_ok.started >= fresh_cut)
    if retired:
        gp.notes.append("retired resource: freshness not checked")
        if is_fresh:
            gp.warns.append(f"marked retired but has a recent snapshot ({fmt_age(now, newest_ok.started)})")
    elif newest_ok is None:
        gp.fails.append("no successful snapshot at all")
    elif not is_fresh:
        gp.fails.append(f"stale: newest successful snapshot is {fmt_age(now, newest_ok.started)} "
                        f"(limit {cfg.get('fresh_within', '7h')})")
    newest = gs[-1]
    if not retired:
        if not is_success(newest):
            gp.fails.append(f"newest snapshot {newest.id[:12]} status {newest.status!r} ({fmt_age(now, newest.started)})")
        elif newest.error_count:
            gp.warns.append(f"newest snapshot {newest.id[:12]} completed with {newest.error_count} error(s)")

    # Retention
    keep_all_cut = ago(now, cfg["keep_all_within"]) if cfg.get("keep_all_within") else None
    tiers = sorted(((ago(now, t["older_than"]), t["keep"]) for t in cfg.get("tiers", [])), key=lambda x: x[0])
    first_in_bucket: dict = {}
    for b in ok_snaps:  # oldest first; only successful snapshots represent a bucket
        for _, kind in tiers:
            first_in_bucket.setdefault((kind, bucket_key(local(b), kind)), b.id)

    reason: dict = {}
    for b in gs:
        if keep_all_cut is None or b.started >= keep_all_cut:
            reason[b.id] = "recent"
            continue
        if not is_success(b):
            continue  # old failed/in-progress records are never kept
        tier = next((kind for cut, kind in tiers if b.started < cut), None)
        if tier is None:
            reason[b.id] = "no tier"  # older than keep_all but no tier applies: keep, to be safe
        elif first_in_bucket[(tier, bucket_key(local(b), tier))] == b.id:
            reason[b.id] = tier

    # Floor: newest N successful snapshots
    keep_last = int(cfg.get("keep_last", 3))
    if keep_last > 0:
        for b in ok_snaps[-keep_last:]:
            reason.setdefault(b.id, "floor")

    gp.reason = reason
    gp.keep = [b for b in gs if b.id in reason]
    gp.delete = [b for b in gs if b.id not in reason]
    return gp


def kept_breakdown(gp: GroupPlan, cfg: dict) -> str:
    """e.g. 'kept: 28 recent, 83 daily, 21 monthly'."""
    order = ["recent"] + [t["keep"] for t in cfg.get("tiers", [])] + ["floor", "no tier"]
    n: dict = {}
    for r in gp.reason.values():
        n[r] = n.get(r, 0) + 1
    return "kept: " + ", ".join(f"{n[r]} {r}" for r in order if n.get(r)) if n else "kept: none"


def plan_all(backups: list, cfg: dict, now: datetime, tz) -> list[GroupPlan]:
    retired = {str(r).lower() for r in cfg.get("retired_resources") or []}
    groups: dict = {}
    for b in backups:
        groups.setdefault((b.resource, b.service), []).append(b)
    return [plan_group(bs, cfg, now, tz, res.lower() in retired)
            for (res, svc), bs in sorted(groups.items(), key=lambda kv: (kv[0][0].lower(), kv[0][1]))]


# ── Actions ──────────────────────────────────────────────────────────────────

def delete_backups(stub: str, service: str, ids: list[str], batch: int) -> tuple[int, list[str]]:
    """Delete by ID in batches. Returns (number deleted, error lines)."""
    done, errors = 0, []
    for i in range(0, len(ids), max(1, batch)):
        chunk = ids[i:i + batch]
        r = corso.run(stub, ["backup", "delete", service, "--backups", ",".join(chunk)])
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip().splitlines()[-3:] or [f"exit code {r.returncode}"]
            errors.append(f"{service}: deleting {len(chunk)} failed: {' | '.join(msg)}")
        else:
            done += len(chunk)
    return done, errors


def repo_size(path: str) -> int | None:
    try:
        r = subprocess.run(["du", "-sb", path], capture_output=True, text=True, timeout=3600)
        return int(r.stdout.split()[0]) if r.returncode == 0 else None
    except Exception:
        return None


def free_space(path: str) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def run_maintenance(script: str, repo_path: str) -> tuple[str, list[str]]:
    lines = []
    size0, free0 = repo_size(repo_path), free_space(repo_path)
    lines.append(f"  before: repo {fmt_gb(size0)}, free {fmt_gb(free0)}")
    start = datetime.now(timezone.utc)
    try:
        r = subprocess.run(["bash", script], capture_output=True, text=True, timeout=MAINT_TIMEOUT)
        rc, out = r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        rc, out = -1, f"timed out after {MAINT_TIMEOUT // 3600}h"
    mins = (datetime.now(timezone.utc) - start).total_seconds() / 60
    size1, free1 = repo_size(repo_path), free_space(repo_path)
    lines.append(f"  after: repo {fmt_gb(size1)}, free {fmt_gb(free1)}  ({mins:.0f} min)")
    if size0 is not None and size1 is not None:
        lines.append(f"  change: {(size1 - size0) / 1e9:+.2f} GB (deletes from the last 24h are reclaimed by next "
                     "week's pass; a pass may grow the repo first)")
    tail = out.splitlines()[-10:]
    if rc != 0:
        lines.append(f"  FAIL maintenance exit code {rc}")
        lines += [f"    {x}" for x in tail]
        return FAIL, lines
    lines.append("  maintenance: ok")
    lines += [f"    {x}" for x in tail]
    return OK, lines


# ── Email (same format and .env keys as restic-tui) ──────────────────────────

def load_env(path: str) -> dict:
    """Minimal .env reader: KEY=VALUE lines, # comments on their own line, optional quotes."""
    env: dict = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                env[k.strip()] = v
    except OSError:
        pass
    return env


def send_email(subject: str, body: str, env: dict) -> str:
    """Send the report. Returns '' on success or an error message. Never logs the password."""
    import smtplib
    import ssl
    from email.message import EmailMessage

    missing = [k for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM", "SMTP_TO") if not env.get(k)]
    if missing:
        return f"email not configured (missing {', '.join(missing)})"
    to = [a.strip() for a in env["SMTP_TO"].split(",") if a.strip()]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = env["SMTP_FROM"]
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    port = int(env["SMTP_PORT"])
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(env["SMTP_HOST"], port, timeout=60, context=ctx)
        else:
            server = smtplib.SMTP(env["SMTP_HOST"], port, timeout=60)
            server.starttls(context=ctx)
        with server:
            if env.get("SMTP_USER"):
                server.login(env["SMTP_USER"], env.get("SMTP_PASS", ""))
            server.send_message(msg, to_addrs=to)
    except Exception as e:
        return f"email failed: {type(e).__name__}: {e}"
    return ""


# ── Main ─────────────────────────────────────────────────────────────────────

def load_prune_config(cfg: dict) -> dict:
    merged = dict(DEFAULT_PRUNE)
    merged.update(cfg.get("prune") or {})
    return merged


def maintenance_due(now_local: datetime, pcfg: dict, args) -> bool:
    if args.no_maintenance:
        return False
    if args.maintenance:
        return True
    day = str(pcfg.get("maintenance_day") or "").strip().lower()
    if day and day not in WEEKDAYS:
        raise ValueError(f"maintenance_day must be a weekday name, not {day!r}")
    return bool(day) and WEEKDAYS[now_local.weekday()] == day


def run_job(args, cfg: dict, pcfg: dict, now: datetime) -> tuple[dict, list[str], list[str], dict, str]:
    """Returns (counts, attention, body, plan listing, maintenance summary line or '').

    The listing ({'state', 'deletes', 'keeps'}) is written to .deletes.txt and .keeps.txt on every run,
    whether the deletes happened, were a dry run, or were blocked by a guard."""
    tz = ZoneInfo(pcfg["timezone"]) if ZoneInfo else timezone.utc
    stub = (cfg.get("scripts") or {}).get("stub", "")
    maint = (cfg.get("scripts") or {}).get("maint", "")
    batch = int(cfg.get("delete_batch_size", 100))
    counts = {FAIL: 0, WARN: 0, SKIPPED: 0, OK: 0}
    attention, body = [], []
    listing = {"state": "", "deletes": [], "keeps": []}
    maint_line = ""

    # List
    backups, list_errors = [], []
    for svc in pcfg["services"]:
        try:
            svc_backups = corso.list_backups(stub, svc)
        except Exception as e:
            list_errors.append(f"cannot list {svc}: {e}")
            continue
        if not svc_backups:
            list_errors.append(f"no backups at all for service {svc}")
        backups += svc_backups
    if args.resource:
        want = args.resource.lower()
        alias = {k.lower(): str(v).lower() for k, v in (cfg.get("users") or {}).items()}
        want = alias.get(want, want)
        backups = [b for b in backups if b.resource.lower() == want]
        list_errors = [e for e in list_errors if not e.startswith("no backups")]
        if not backups:
            list_errors.append(f"no backups for resource {args.resource!r}")
    for e in list_errors:
        counts[FAIL] += 1
        attention.append(f"FAIL     {e}")
        body.append(f"  FAIL {e}")

    # Plan
    plans = plan_all(backups, pcfg, now, tz)
    total = len(backups)
    to_delete = [b for gp in plans for b in gp.delete]
    for gp in plans:
        st = FAIL if gp.fails else WARN if gp.warns else OK
        counts[st] += 1
        name = f"{gp.resource} / {gp.service}"
        for m in gp.fails:
            attention.append(f"FAIL     {name}: {m}")
        for m in gp.warns:
            attention.append(f"WARN     {name}: {m}")
        body.append(f"== {name}  {st}")
        body.append(f"  snapshots {len(gp.snaps)}, keep {len(gp.keep)}, delete {len(gp.delete)}")
        body.append(f"  {kept_breakdown(gp, pcfg)}")
        if gp.keep:
            o, n = gp.keep[0], gp.keep[-1]
            body.append(f"  oldest kept {o.started.astimezone(tz):%Y-%m-%d %H:%M} ({fmt_age(now, o.started)})")
            body.append(f"  newest kept {n.started.astimezone(tz):%Y-%m-%d %H:%M} ({fmt_age(now, n.started)})")
        last = gp.snaps[-1]
        status = last.status + (f" ({last.error_count} errors)" if last.error_count else "")
        body.append(f"  newest snapshot: {status}, {fmt_age(now, last.started)}")
        for m in gp.notes:
            body.append(f"  note: {m}")
        for m in gp.warns:
            body.append(f"  WARN {m}")
        for m in gp.fails:
            body.append(f"  FAIL {m}")
        body.append("")

    # Deletion cap and decision
    blocked = bool(list_errors) or any(gp.fails for gp in plans)
    pct = 100.0 * len(to_delete) / total if total else 0.0
    cap = float(pcfg.get("max_delete_percent", 20))
    body.append("== Deletes")
    if pct > cap and not args.no_cap:
        blocked = True
        counts[FAIL] += 1
        m = f"deletion cap: would delete {len(to_delete)} of {total} ({pct:.0f}% > {cap:.0f}%); rerun with --no-cap if expected"
        attention.append(f"FAIL     {m}")
        body.append(f"  FAIL {m}")
    row = lambda b, why="": (f"{b.service}\t{b.resource}\t{b.started.astimezone(tz):%Y-%m-%d %H:%M}\t"  # noqa: E731
                             f"{b.status}{f' ({b.error_count} errors)' if b.error_count else ''}\t{why}\t{b.id}")
    listing["deletes"] = [row(b) for b in to_delete]
    listing["keeps"] = [row(b, gp.reason[b.id]) for gp in plans for b in gp.keep]
    if blocked:
        listing["state"] = "NOT deleted: blocked by a guard (see report)"
        body.append(f"  nothing deleted (guard failed); {len(to_delete)} snapshot(s) would have been deleted")
    elif not to_delete:
        body.append("  nothing to delete")
    elif args.dry_run:
        listing["state"] = "NOT deleted: dry run"
        body.append(f"  dry-run: would delete {len(to_delete)} of {total} snapshot(s) ({pct:.0f}%)")
    else:
        listing["state"] = "deleted (any per-service errors are in the report)"
        by_svc: dict = {}
        for b in to_delete:
            by_svc.setdefault(b.service, []).append(b)
        for svc, bs in sorted(by_svc.items()):
            n, errs = delete_backups(stub, svc, [b.id for b in bs], batch)
            body.append(f"  {svc}: deleted {n} of {len(bs)}")
            for e in errs:
                counts[FAIL] += 1
                attention.append(f"FAIL     delete {e}")
                body.append(f"  FAIL {e}")
    body.append("")

    # Maintenance (never deletes referenced data, so it runs even if deletes were blocked)
    if maintenance_due(now.astimezone(tz), pcfg, args):
        body.append("== Maintenance")
        if args.dry_run:
            body.append(f"  dry-run: would run {maint}")
            maint_line = "Maintenance: would run (dry-run)"
        elif not maint:
            st, lines = FAIL, ["  FAIL scripts.maint is not set in config.yaml"]
            counts[FAIL] += 1
            attention.append("FAIL     maintenance: scripts.maint is not set")
            body += lines
        else:
            st, lines = run_maintenance(maint, pcfg["repo_path"])
            counts[st] += 1
            body += lines
            if st == FAIL:
                attention.append(f"FAIL     maintenance: {next(x.strip() for x in lines if 'FAIL' in x)[5:]}")
            maint_line = f"Maintenance: {st}; " + "; ".join(x.strip() for x in lines[:2])
        body.append("")
    return counts, attention, body, listing, maint_line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check Corso backups, thin snapshots, run weekly maintenance, report.")
    ap.add_argument("--dry-run", action="store_true", help="report only; change nothing")
    ap.add_argument("--no-cap", action="store_true", help="disable the deletion cap (initial cleanup)")
    ap.add_argument("--no-email", action="store_true", help="write the report but do not email it")
    ap.add_argument("--resource", help="only this user/site (name or config 'users' alias)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--maintenance", action="store_true", help="run maintenance today regardless of maintenance_day")
    g.add_argument("--no-maintenance", action="store_true", help="skip maintenance even on maintenance_day")
    ap.add_argument("--config", default=CONFIG_PATH)
    args = ap.parse_args(argv)

    import yaml
    with open(args.config) as f:
        cfg = yaml.safe_load(f) or {}
    pcfg = load_prune_config(cfg)
    now = datetime.now(timezone.utc)

    # Single runner: maintenance uses --force, which drops Corso's own overlap protection
    lock = open(pcfg["lock_file"], "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
    except OSError:
        locked = False

    if not locked:
        counts = {FAIL: 0, WARN: 0, SKIPPED: 1, OK: 0}
        attention = ["SKIPPED  another prune run holds the lock; nothing done"]
        body, listing, maint_line = [], None, ""
    else:
        try:
            counts, attention, body, listing, maint_line = run_job(args, cfg, pcfg, now)
        except Exception:
            counts = {FAIL: 1, WARN: 0, SKIPPED: 0, OK: 0}
            attention = ["FAIL     unexpected error (see details)"]
            body = ["FAIL unexpected error:"] + [f"  {x}" for x in traceback.format_exc().splitlines()]
            listing, maint_line = None, ""

    flags = [x for x, on in (("dry-run", args.dry_run), ("no-cap", args.no_cap)) if on]
    if args.resource:
        flags.append(f"resource {args.resource}")
    header = f"[corso] FAIL {counts[FAIL]} / WARN {counts[WARN]} / SKIPPED {counts[SKIPPED]} / OK {counts[OK]}"
    if flags:
        header += f" ({', '.join(flags)})"
    top = ["Summary", "-------"]
    top += (["Needs attention:"] + [f"  {x}" for x in attention]) if attention else ["Everything OK."]
    if maint_line:
        top.append(maint_line)
    top += ["", "Details", "-------", ""]
    report = "\n".join([header, f"run at {now:%Y-%m-%d %H:%M:%S} UTC", ""] + top + body)

    email_err = ""
    if args.no_email:
        report += "\nemail: skipped (--no-email)"
    else:
        env = load_env(pcfg["env_file"])
        env.update({k: v for k, v in os.environ.items() if k.startswith("SMTP_")})
        email_err = send_email(header, report, env)
        report += f"\nemail: {email_err or 'sent to ' + env.get('SMTP_TO', '')}"

    os.makedirs(pcfg["report_dir"], exist_ok=True)
    stem = f"{now:%Y-%m-%d_%H%M%S}" + ("-dry-run" if args.dry_run else "")
    report_path = os.path.join(pcfg["report_dir"], f"{stem}.txt")
    with open(report_path, "w") as f:
        f.write(report + "\n")
    if listing and (listing["deletes"] or listing["keeps"]):
        cols = "service\tresource\tstarted (local)\tstatus\t{}\tid\n"
        for kind, extra in (("deletes", ""), ("keeps", "kept because")):
            rows = listing[kind]
            state = listing["state"] if kind == "deletes" else "kept"
            with open(os.path.join(pcfg["report_dir"], f"{stem}.{kind}.txt"), "w") as f:
                f.write(f"# {len(rows)} snapshot(s), {state}\n" + cols.format(extra) + "\n".join(rows) + ("\n" if rows else ""))
    with open(pcfg["last_report"], "w") as f:
        f.write(report + "\n")

    print(report)
    print(f"\nReport written to {report_path}")
    if email_err:
        print(email_err, file=sys.stderr)
    return 1 if counts[FAIL] or (email_err and not email_err.startswith("email not configured")) else 0


if __name__ == "__main__":
    sys.exit(main())
