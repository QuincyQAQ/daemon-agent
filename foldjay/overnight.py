"""One authorized Slurm submission per run, recoverable from queue and accounting."""
from __future__ import annotations

import pwd
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .morning import morning_report, read_record, render_report, validate_plan
from .permissions import Policy
from .storage import atomic_write

TERMINAL = {"BOOT_FAIL", "CANCELLED", "COMPLETED", "DEADLINE", "FAILED", "NODE_FAIL",
            "OUT_OF_MEMORY", "PREEMPTED", "REVOKED", "SPECIAL_EXIT", "TIMEOUT"}


def add_parser(commands):
    parser = commands.add_parser("overnight", help="Submit and recover an authorized Slurm experiment")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("start", "status", "watch"):
        sub = actions.add_parser(action)
        sub.add_argument("run_id", help="Stable local identifier; reuse it after an interruption")
        if action == "start":
            sub.add_argument("experiment", help="Input directory with experiment.json")
            sub.add_argument("--script", required=True, help="Trusted Bash workload, relative to workspace")
            sub.add_argument("--partition", required=True)
            sub.add_argument("--qos")
            sub.add_argument("--cpus", type=int, default=1)
            sub.add_argument("--gpus", type=int, default=0)
            sub.add_argument("--mem-mib", type=int, default=512)
            sub.add_argument("--minutes", type=int, default=5)
            sub.add_argument("--watch", action="store_true", help="Wait for accounting and write the morning report")
        if action != "status":
            sub.add_argument("--interval", type=int, default=30, help="Seconds between scheduler queries")


class SlurmError(RuntimeError):
    pass


def slurm(argv, *, script=None):
    # SBATCH_* overrides from an unrelated login session must not affect this allocation.
    env = {k: v for k, v in os.environ.items() if not k.startswith("SBATCH_") and k != "SLURM_CLUSTERS"}
    result = subprocess.run(argv, input=script, capture_output=True, text=True,
                            timeout=45, env=env)
    if result.returncode:
        raise SlurmError(f"{argv[0]} exited {result.returncode}: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def cluster_name():
    match = re.search(r"^ClusterName\s*=\s*(\S+)", slurm(["scontrol", "show", "config"]), re.M)
    if not match:
        raise ValueError("Could not identify Slurm cluster")
    return match.group(1)


def checkpoint(folder, record):
    atomic_write(folder / "submission.json", json.dumps(record, indent=2) + "\n")
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare(args, config):
    policy = Policy(config.workspace, config.state, "plan")
    source = policy.path(args.experiment)
    manifest = policy.path(str(source / "experiment.json"))
    # Validate the comparison plan before reserving compute.
    plan = read_record(policy, manifest)
    validate_plan(plan)
    script = policy.path(args.script)
    if not script.is_file():
        raise ValueError("Expected a regular Bash workload file")
    body = script.read_text(encoding="utf-8")
    if not body.strip() or "\x00" in body:
        raise ValueError("Empty or invalid Bash workload")
    if min(args.cpus, args.mem_mib, args.minutes) < 1 or args.gpus < 0:
        raise ValueError("Invalid CPU, GPU, memory or time limit")
    for value in (args.partition, args.qos):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("Invalid partition or QoS")
    return dict(input=str(source), script=str(script), script_sha256=hashlib.sha256(body.encode()).hexdigest(),
                plan=plan, partition=args.partition, qos=args.qos, cpus=args.cpus, gpus=args.gpus,
                mem_mib=args.mem_mib, minutes=args.minutes), body


def submit(folder, rec):
    spec = rec["authorization"]
    cmd = ["sbatch", "--parsable", "--job-name=" + rec["token"], "--nodes=1", "--ntasks=1",
           f"--partition={spec['partition']}", f"--cpus-per-task={spec['cpus']}",
           f"--mem={spec['mem_mib']}M", f"--time={spec['minutes']}",
           "--chdir=" + str(folder / "output"), "--output=" + str(folder / "slurm-%j.out")]
    if spec["qos"]:
        cmd.append("--qos=" + spec["qos"])
    if spec["gpus"]:
        cmd.append(f"--gpus={spec['gpus']}")
    wrapper = ("#!/bin/bash\nset -eu\nexport DAEMON_INPUT_DIR=" + shlex.quote(spec["input"])
               + "\nexec bash " + shlex.quote(str(folder / "workload.sh")) + "\n")
    try:
        result = slurm(cmd, script=wrapper)
    except (SlurmError, subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"{exc}. Submission outcome unknown for {folder.name}. "
                           "Use overnight status with the same run ID.") from exc
    match = re.fullmatch(r"(\d+)(?:;([^;\s]+))?", result)
    if not match or (match[2] and match[2] != rec["cluster"]):
        raise ValueError("Unconfirmed sbatch response. Use overnight status to reconcile the saved intent.")
    rec.update(job_id=match[1], state="SUBMITTED")
    checkpoint(folder, rec)


def reconcile(folder, rec):
    token, user = rec["token"], rec["user"]
    queue = slurm(["squeue", "--noheader", "--user", user, "--name", token, "--format=%i|%j|%T"])
    history = slurm(["sacct", "-X", "-nP", "--user", user, "--name", token, "--starttime", rec["since"],
                     "--format=JobIDRaw,JobName%64,State%40,ExitCode"])
    matches = {}
    for text, accounting in ((history, True), (queue, False)):
        for line in text.splitlines():
            fields = [part.strip() for part in line.split("|")]
            if len(fields) != (4 if accounting else 3) or fields[1] != token:
                continue
            ident, _, state = fields[:3]
            if not ident.isdigit():
                raise ValueError("Unexpected non-scalar Slurm job identifier")
            matches[ident] = (state.split()[0], fields[3] if accounting else None)
    if len(matches) > 1 or (matches and rec.get("job_id") and rec["job_id"] not in matches):
        raise ValueError("Conflicting Slurm identifiers. Inspect the job records before proceeding.")
    if matches:
        ident, (state, exit_code) = next(iter(matches.items()))
        rec.update(job_id=ident, state=state, exit_code=exit_code)
        if state in TERMINAL and exit_code:
            # Consult accounting before finalizing; squeue can still show a completing job.
            output = folder / "output"
            try:
                policy = Policy(output, output / ".foldjay", "plan")
                report = morning_report(policy, ".")
                if report["manifest_sha256"] != rec["manifest_sha256"]:
                    raise ValueError("Workload changed the authorized experiment.json")
            except (OSError, ValueError) as exc:
                rec["report_error"] = str(exc)
                report = {"error": str(exc), "pairs": []}
            if state != "COMPLETED" or exit_code != "0:0":
                report["pairs"] = []
            report["slurm"] = {"job_id": ident, "state": state, "exit_code": exit_code}
            text = report.get("error") or render_report(report)
            atomic_write(folder / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
            atomic_write(folder / "report.txt", f"Slurm {ident}  {state}  {exit_code}\n" + text + "\n")
            rec["report"] = str(folder / "report.txt")
        checkpoint(folder, rec)
    else:
        rec["state"] = "AWAITING_CONFIRMATION"
        checkpoint(folder, rec)


def command(args, config, store):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", args.run_id):
        raise ValueError("Run ID must contain 1–48 letters, digits, dashes or underscores")
    if getattr(args, "interval", 1) < 1:
        raise ValueError("Polling interval must be positive")
    folder = store.root / "overnight" / args.run_id
    path = folder / "submission.json"
    spec = prepare(args, config) if args.action == "start" else None
    identity = {"cluster": cluster_name(), "user": pwd.getpwuid(os.getuid()).pw_name, "workspace": str(config.workspace)}
    watch = args.action == "watch" or getattr(args, "watch", False)
    while True:
        with store.session_lock("overnight-" + args.run_id, wait=True):
            if path.exists():
                rec = json.loads(path.read_text())
                if any(rec[k] != v for k, v in identity.items()):
                    raise ValueError("Run belongs to a different cluster, user or workspace")
                if spec and rec["authorization"] != spec[0]:
                    raise ValueError("Run ID already has a different workload or resource authorization")
                if not rec.get("report"):
                    try:
                        reconcile(folder, rec)
                    except (SlurmError, subprocess.TimeoutExpired) as exc:
                        if not watch:
                            raise
                        print(f"Scheduler unavailable, retrying: {exc}", file=sys.stderr, flush=True)
            else:
                if spec is None:
                    raise ValueError("Unknown overnight run")
                if not shutil.which("sbatch"):
                    raise ValueError("sbatch is unavailable")
                auth, body = spec
                out = folder / "output"
                out.mkdir(parents=True, exist_ok=True)
                manifest_text = json.dumps(auth["plan"])
                atomic_write(out / "experiment.json", manifest_text)
                atomic_write(folder / "workload.sh", body)
                rec = dict(identity, token="dj-" + uuid.uuid4().hex[:24], authorization=auth,
                           since=(datetime.now()-timedelta(days=1)).strftime("%Y-%m-%d"),
                           state="INTENT", job_id=None, manifest_sha256=hashlib.sha256(manifest_text.encode()).hexdigest())
                # This must reach disk before sbatch. Every later invocation queries Slurm.
                checkpoint(folder, rec)
                submit(folder, rec)
        if not watch or rec.get("report"):
            return rec
        time.sleep(args.interval)
