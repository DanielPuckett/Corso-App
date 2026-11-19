"""Tests for prune_job planning, guards, deletes, maintenance and report. No Corso or Docker needed.

Run: python3 tests/test_prune_plan.py
"""
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Python < 3.10 can't evaluate "str | None" annotations; load corso.py with postponed annotations.
if sys.version_info < (3, 10):
    src = open(os.path.join(ROOT, "corso.py")).read()
    mod = types.ModuleType("corso")
    mod.__file__ = os.path.join(ROOT, "corso.py")
    sys.modules["corso"] = mod
    exec(compile("from __future__ import annotations\n" + src, mod.__file__, "exec"), mod.__dict__)

import corso  # noqa: E402
import prune_job as pj  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

TZ = ZoneInfo("America/Toronto")
CFG = pj.load_prune_config({})
# Wednesday 2026-11-18 06:45 local, just after the 06:30 run
NOW = datetime(2026, 11, 18, 6, 45, tzinfo=TZ).astimezone(timezone.utc)
_n = [0]
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def rec(local_dt, resource="me@example.com", service="onedrive", status="Completed", errors=0):
    _n[0] += 1
    return corso.BackupRecord(id=f"{_n[0]:08x}-0000-0000-0000-000000000000", service=service, resource=resource,
                              started=local_dt.astimezone(timezone.utc), status=status, error_count=errors)


def six_hourly(start_local, end_local, **kw):
    out, d = [], start_local
    while d <= end_local:
        for h in (0, 6, 12, 18):
            t = d.replace(hour=h, minute=30)
            if start_local <= t <= end_local:
                out.append(rec(t, **kw))
        d += timedelta(days=1)
    return out


def local(b):
    return b.started.astimezone(TZ)


# ── JSON parsing ─────────────────────────────────────────────────────────────
sample = json.dumps([
    {"id": "c3d761f4-cb04-4f0e-9a8e-111111111111", "status": "Completed", "version": "0",
     "protectedResourceID": "abc-123", "protectedResourceName": "me@example.com", "owner": "abc-123",
     "stats": {"id": "c3d761f4-cb04-4f0e-9a8e-111111111111", "errorCount": 2, "startedAt": "2026-11-18T17:30:01.582210415Z"}},
    {"id": "de1f8f2f-1882-4a4b-8f51-222222222222", "status": "Failed", "protectedResourceID": "def-456",
     "stats": {"errorCount": 0, "startedAt": "2026-11-18T17:30:02.5Z"}},
])
parsed = corso.parse_backup_list_json("exchange", "progress noise\n" + sample + "\n")
check(len(parsed) == 2 and parsed[0].resource == "me@example.com" and parsed[0].error_count == 2,
      "JSON: name, error count parsed; surrounding noise ignored")
check(parsed[1].resource == "def-456" and parsed[1].status == "Failed", "JSON: falls back to protectedResourceID")
check(parsed[0].started == datetime(2026, 11, 18, 17, 30, 1, 582210, tzinfo=timezone.utc)
      and parsed[1].started.microsecond == 500000, "JSON: nanosecond and short fractions parsed as UTC")
check(corso.parse_backup_list_json("onedrive", "") == [], "JSON: empty output means no backups")

# ── Retention over 6-hourly history since April ──────────────────────────────
hist = six_hourly(datetime(2026, 4, 25, tzinfo=TZ), datetime(2026, 11, 18, 6, 30, tzinfo=TZ))
gp = pj.plan_group(hist, CFG, NOW, TZ, retired=False)
kept = {b.id for b in gp.keep}
cut7, cut3m = pj.ago(NOW, "7d"), pj.ago(NOW, "3M")

check(all(b.id in kept for b in hist if b.started >= cut7), "everything within 7 days kept")
mid = [b for b in hist if cut3m <= b.started < cut7]
mid_kept = [b for b in mid if b.id in kept]
check(all(local(b).hour == 0 for b in mid_kept), "7d-3M: only the 00:30 local run kept (first of local day)")
days_in_window = {local(b).date() for b in mid if local(b).hour == 0}  # days whose 00:30 run is in the window
check({local(b).date() for b in mid_kept} == days_in_window and len(mid_kept) == len(days_in_window),
      f"7d-3M: exactly one per day ({len(mid_kept)} days)")
old_kept = [b for b in hist if b.started < cut3m and b.id in kept]
check([(local(b).month, local(b).day, local(b).hour) for b in old_kept] == [(4, 25, 0), (5, 1, 0), (6, 1, 0), (7, 1, 0), (8, 1, 0)],
      f"over 3M: first of each month: {[local(b).strftime('%m-%d %H:%M') for b in old_kept]}")
check(80 <= len(gp.keep) <= 130, f"about 115 kept per group ({len(gp.keep)} of {len(hist)})")
check(not gp.fails and not gp.warns, "fresh healthy group: no guard findings")
bd = pj.kept_breakdown(gp, CFG)
n_recent = sum(1 for b in hist if b.started >= cut7)
check(bd == f"kept: {n_recent} recent, {len(mid_kept)} daily, {len(old_kept)} monthly", f"breakdown: {bd}")
check(set(gp.reason) == {b.id for b in gp.keep} and set(gp.reason.values()) <= {"recent", "daily", "monthly", "floor", "no tier"},
      "every kept snapshot has a reason")

# Stable from one day to the next (only aging drops a kept snapshot)
tomorrow = pj.plan_group(hist, CFG, NOW + timedelta(days=1), TZ, retired=False)
dropped = {b.id for b in gp.keep} - {b.id for b in tomorrow.keep}
check(len(dropped) <= 4, f"next day drops only aged snapshots ({len(dropped)})")

# Failed snapshots: never a bucket representative, deleted once older than 7 days
fhist = six_hourly(datetime(2026, 9, 1, tzinfo=TZ), datetime(2026, 11, 18, 6, 30, tzinfo=TZ))
for b in fhist:
    if local(b).date() == datetime(2026, 10, 15).date() and local(b).hour == 0:
        b.status = "Failed"
    if local(b).date() == datetime(2026, 11, 16).date() and local(b).hour == 0:
        b.status = "Failed"
gpf = pj.plan_group(fhist, CFG, NOW, TZ, retired=False)
oct15 = [b for b in gpf.keep if local(b).date() == datetime(2026, 10, 15).date()]
check(len(oct15) == 1 and local(oct15[0]).hour == 6, "failed 00:30 run: the day's 06:30 run represents the day")
check(any(local(b).date() == datetime(2026, 11, 16).date() and b.status == "Failed" for b in gpf.keep),
      "failed run within 7 days: kept (everything within 7 days)")
check(not any(b.status == "Failed" and b.started < cut7 for b in gpf.keep), "old failed runs deleted")

# Floor counts successful snapshots only
tail = six_hourly(datetime(2026, 10, 1, tzinfo=TZ), datetime(2026, 10, 3, 18, 30, tzinfo=TZ))
for b in tail[-2:]:
    b.status = "Failed"
gpt = pj.plan_group(tail, CFG, NOW, TZ, retired=True)
ok_newest = [b for b in tail if b.status == "Completed"][-3:]
check(all(b in gpt.keep for b in ok_newest), "floor: newest 3 successful kept even when newer runs failed")
check("floor" in pj.kept_breakdown(gpt, CFG), f"floor shows in breakdown ({pj.kept_breakdown(gpt, CFG)})")

# ── Guards ───────────────────────────────────────────────────────────────────
stale = six_hourly(datetime(2026, 11, 1, tzinfo=TZ), datetime(2026, 11, 17, 18, 30, tzinfo=TZ))
gps = pj.plan_group(stale, CFG, NOW, TZ, retired=False)
check(any("stale" in f for f in gps.fails), f"freshness: last run 12h ago fails 7h limit ({gps.fails})")

late = six_hourly(datetime(2026, 11, 10, tzinfo=TZ), datetime(2026, 11, 18, 0, 30, tzinfo=TZ))
gpl = pj.plan_group(late, CFG, NOW, TZ, retired=False)
check(not gpl.fails, "freshness: missing only the 06:30 run (6.25h) is still within 7h")

bad = six_hourly(datetime(2026, 11, 10, tzinfo=TZ), datetime(2026, 11, 18, 6, 30, tzinfo=TZ))
bad[-1].status = "Failed"
gpb = pj.plan_group(bad, CFG, NOW, TZ, retired=False)
check(any("status 'Failed'" in f for f in gpb.fails), "status: newest Failed -> FAIL")
bad[-1].status = "In Progress"
check(any("In Progress" in f for f in pj.plan_group(bad, CFG, NOW, TZ, retired=False).fails), "status: newest In Progress -> FAIL")
bad[-1].status, bad[-1].error_count = "Completed", 3
gpe = pj.plan_group(bad, CFG, NOW, TZ, retired=False)
check(not gpe.fails and any("3 error" in w for w in gpe.warns), "status: Completed with errors -> WARN")
bad[-1].status, bad[-1].error_count = "No Data", 0
check(not pj.plan_group(bad, CFG, NOW, TZ, retired=False).fails, "status: No Data counts as successful")

gpr = pj.plan_group(stale, CFG, NOW, TZ, retired=True)
check(not gpr.fails and gpr.delete, "retired: no freshness failure, retention still applies")
gprf = pj.plan_group(hist, CFG, NOW, TZ, retired=True)
check(any("recent snapshot" in w for w in gprf.warns), "retired with a fresh snapshot -> WARN")

plans = pj.plan_all(stale + hist, dict(CFG, retired_resources=["ME@example.com"]), NOW, TZ)
check(all(not p.fails for p in plans), "retired_resources matches case-insensitively")

# ── run_job with a fake corso ────────────────────────────────────────────────
tmp = tempfile.mkdtemp()


class Fake:
    def __init__(self, by_svc, list_fail=(), delete_fail=(), maint_rc=0):
        self.by_svc, self.list_fail, self.delete_fail, self.maint_rc = by_svc, set(list_fail), set(delete_fail), maint_rc
        self.deleted, self.maint_runs = {}, 0

    def install(self):
        def list_backups(stub, svc):
            if svc in self.list_fail:
                raise RuntimeError("connection refused")
            return list(self.by_svc.get(svc, []))

        def run(script, args, timeout=1200):
            if args[:2] == ["backup", "delete"]:
                svc, ids = args[2], args[4].split(",")
                rc = 1 if svc in self.delete_fail else 0
                if not rc:
                    self.deleted.setdefault(svc, []).extend(ids)
                return types.SimpleNamespace(returncode=rc, stdout="", stderr="boom" if rc else "")
            raise AssertionError(args)

        corso.list_backups, corso.run = list_backups, run
        pj.subprocess = types.SimpleNamespace(
            run=lambda cmd, **kw: (setattr(self, "maint_runs", self.maint_runs + 1) or
                                   types.SimpleNamespace(returncode=self.maint_rc, stdout="maintenance done", stderr=""))
            if cmd[0] == "bash" else types.SimpleNamespace(returncode=0, stdout="1000000000\t/x", stderr=""),
            TimeoutExpired=TimeoutError)
        return self


def args(**kw):
    base = dict(dry_run=False, no_cap=False, no_email=True, resource=None, maintenance=False, no_maintenance=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def healthy(resource):
    return {svc: six_hourly(datetime(2026, 4, 25, tzinfo=TZ), datetime(2026, 11, 18, 6, 30, tzinfo=TZ),
                            resource=resource, service=svc) for svc in ("exchange", "onedrive", "sharepoint")}


def merged(*dicts):
    out = {}
    for d in dicts:
        for k, v in d.items():
            out.setdefault(k, []).extend(v)
    return out


data = merged(healthy("me@example.com"), healthy("friend@example.org"))
cfgx = {"scripts": {"stub": "/stub.sh", "maint": "/maint.sh"}, "delete_batch_size": 100,
        "users": {"friend": "friend@example.org"}}
pcfg = dict(CFG, repo_path=tmp)

f = Fake(data).install()
counts, att, body, dels, ml = pj.run_job(args(), cfgx, pcfg, NOW)
check(counts[pj.FAIL] == 1 and any("deletion cap" in a for a in att) and not f.deleted, "initial backlog: deletion cap blocks")

f = Fake(data).install()
counts, att, body, dels, ml = pj.run_job(args(dry_run=True, no_cap=True), cfgx, pcfg, NOW)
check(counts == {pj.FAIL: 0, pj.WARN: 0, pj.SKIPPED: 0, pj.OK: 6} and not f.deleted and dels["deletes"], "dry-run --no-cap: 6 OK groups, nothing deleted, list written")

f = Fake(data).install()
counts, att, body, dels, ml = pj.run_job(args(no_cap=True), cfgx, pcfg, NOW)
n_expected = sum(len(pj.plan_group(v, CFG, NOW, TZ, False).delete) for k, v in
                 {(b.resource, b.service): [x for x in sum(data.values(), []) if (x.resource, x.service) == (b.resource, b.service)]
                  for b in sum(data.values(), [])}.items())
check(sum(len(v) for v in f.deleted.values()) == n_expected and set(f.deleted) == {"exchange", "onedrive", "sharepoint"},
      f"--no-cap: deletes per service ({sum(len(v) for v in f.deleted.values())} of {n_expected} planned)")
check(f.maint_runs == 0 and not ml, "Wednesday: no maintenance")

after = {svc: [b for b in bs if b.id not in set(f.deleted.get(svc, []))] for svc, bs in data.items()}
f2 = Fake(after).install()
counts, att, body, dels, ml = pj.run_job(args(), cfgx, pcfg, NOW)
check(counts[pj.FAIL] == 0 and not f2.deleted and any("nothing to delete" in x for x in body), "second run: nothing left to delete")

f = Fake(data, list_fail=["sharepoint"]).install()
counts, att, body, dels, ml = pj.run_job(args(no_cap=True), cfgx, pcfg, NOW)
check(any("cannot list sharepoint" in a for a in att) and not f.deleted, "list failure: FAIL and nothing deleted")

stale_data = merged(data, {"onedrive": stale[:0]})
stale_data["onedrive"] = [b for b in stale_data["onedrive"] if not (b.resource == "friend@example.org" and b.started > pj.ago(NOW, "12h"))]
f = Fake(stale_data).install()
counts, att, body, dels, ml = pj.run_job(args(no_cap=True), cfgx, pcfg, NOW)
check(any("friend@example.org / onedrive: stale" in a for a in att) and not f.deleted, "one stale group blocks all deletes")

f = Fake(data, delete_fail=["onedrive"]).install()
counts, att, body, dels, ml = pj.run_job(args(no_cap=True), cfgx, pcfg, NOW)
check(any(a.startswith("FAIL     delete onedrive") for a in att) and "exchange" in f.deleted, "delete error on one service: FAIL, others proceed")

f = Fake(data).install()
counts, att, body, dels, ml = pj.run_job(args(resource="friend", dry_run=True, no_cap=True), cfgx, pcfg, NOW)
check(counts[pj.OK] == 3 and all("friend@example.org" in d for d in dels["deletes"] + dels["keeps"]), "--resource alias limits to that user's 3 groups")

# Maintenance
f = Fake(after).install()
counts, att, body, dels, ml = pj.run_job(args(maintenance=True), cfgx, pcfg, NOW)
check(f.maint_runs == 1 and ml.startswith("Maintenance: OK") and any("before: repo" in x for x in body), f"--maintenance runs the script ({ml})")
sunday = datetime(2026, 11, 22, 6, 45, tzinfo=TZ).astimezone(timezone.utc)
check(pj.maintenance_due(sunday.astimezone(TZ), pcfg, args()) and not pj.maintenance_due(NOW.astimezone(TZ), pcfg, args()),
      "maintenance_day sunday: due on Sunday only")
check(not pj.maintenance_due(sunday.astimezone(TZ), pcfg, args(no_maintenance=True)), "--no-maintenance skips Sunday")
f = Fake(after, maint_rc=1).install()
counts, att, body, dels, ml = pj.run_job(args(maintenance=True), cfgx, pcfg, NOW)
check(counts[pj.FAIL] == 1 and any("maintenance exit code 1" in a for a in att), "maintenance failure -> FAIL in summary")
f = Fake(after).install()
counts, att, body, dels, ml = pj.run_job(args(maintenance=True, dry_run=True), cfgx, pcfg, NOW)
check(f.maint_runs == 0 and "would run" in ml, "dry-run: maintenance not run")
f = Fake(after).install()
counts, att, body, dels, ml = pj.run_job(args(maintenance=True), dict(cfgx, scripts={"stub": "/stub.sh"}), pcfg, NOW)
check(counts[pj.FAIL] == 1 and f.maint_runs == 0, "maintenance without scripts.maint -> FAIL")

# ── main(): lock, report, header ─────────────────────────────────────────────
fake_yaml = types.ModuleType("yaml")
fake_yaml.safe_load = lambda fh: dict(cfgx, prune={"report_dir": os.path.join(tmp, "reports"),
                                                   "last_report": os.path.join(tmp, "last.txt"),
                                                   "lock_file": os.path.join(tmp, "prune.lock"),
                                                   "repo_path": tmp, "env_file": os.path.join(tmp, ".env")})
sys.modules["yaml"] = fake_yaml
open(os.path.join(tmp, "config.yaml"), "w").close()
pj.datetime = type("D", (datetime,), {"now": staticmethod(lambda tz=None: NOW)})

Fake(after).install()
rc = pj.main(["--config", os.path.join(tmp, "config.yaml"), "--no-email"])
last = open(os.path.join(tmp, "last.txt")).read()
check(last.startswith("[corso] FAIL 0 / WARN 0 / SKIPPED 0 / OK 6") and "Everything OK." in last and rc == 0,
      f"main: clean header and summary ({last.splitlines()[0]})")

Fake(data).install()
rc = pj.main(["--config", os.path.join(tmp, "config.yaml"), "--no-email", "--dry-run"])
last = open(os.path.join(tmp, "last.txt")).read()
check(rc == 1 and "Needs attention:" in last and "deletion cap" in last.split("Details")[0], "main: cap FAIL in summary, exit 1")
rdir = os.path.join(tmp, "reports")
dfile = max((n for n in os.listdir(rdir) if n.endswith("-dry-run.deletes.txt")), default="")
kfile = dfile.replace(".deletes.txt", ".keeps.txt")
check(bool(dfile) and os.path.exists(os.path.join(rdir, kfile)), "blocked dry run still writes .deletes.txt and .keeps.txt")
dl = open(os.path.join(rdir, dfile)).read().splitlines()
kl = open(os.path.join(rdir, kfile)).read().splitlines()
check(dl[0].startswith("# ") and "blocked by a guard" in dl[0], f"deletes header says it was blocked ({dl[0]})")
total_rows = len(dl) - 2 + len(kl) - 2
check(total_rows == sum(len(v) for v in data.values()), f"deletes + keeps = every snapshot ({total_rows})")
ids_d = {x.split("\t")[-1] for x in dl[2:]}
ids_k = {x.split("\t")[-1] for x in kl[2:]}
check(not ids_d & ids_k, "no snapshot is in both lists")
check(all(x.split("\t")[4] in ("recent", "daily", "monthly", "floor", "no tier") for x in kl[2:]), "keeps file gives a reason per row")
check("kept: " in last, "report shows the kept breakdown per group")

import fcntl  # noqa: E402
holder = open(os.path.join(tmp, "prune.lock"), "a+")
fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
f = Fake(data).install()
rc = pj.main(["--config", os.path.join(tmp, "config.yaml"), "--no-email", "--no-cap"])
last = open(os.path.join(tmp, "last.txt")).read()
check("SKIPPED 1" in last.splitlines()[0] and not f.deleted, "lock held: SKIPPED, nothing done")
fcntl.flock(holder, fcntl.LOCK_UN)

print("\n----- sample report (truncated) -----")
Fake(after).install()
pj.main(["--config", os.path.join(tmp, "config.yaml"), "--no-email", "--maintenance"])
print("\n".join(open(os.path.join(tmp, "last.txt")).read().splitlines()[:24]))

print()
print(f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
