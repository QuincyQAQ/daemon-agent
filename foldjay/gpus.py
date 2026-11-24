"""Read GPU inventory from one Slurm controller. Counts refer to configured GPU GRES."""
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def query(argv, host, socket):
    if host:
        if not re.fullmatch(r"(?:[\w.-]+@)?[\w.-]+", host) or host.startswith("-"):
            raise ValueError("Invalid SSH host. Use a configured SSH alias or user@hostname.")
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
        if socket:
            cmd.extend(["-S", socket])
        cmd.extend(["--", host, 'PATH=/opt/slurm/bin:$PATH LC_ALL=C ' + shlex.join(argv)])
    else:
        binary = Path("/opt/slurm/bin") / argv[0]
        cmd = [str(binary) if binary.is_file() else argv[0], *argv[1:]]
    env = {**os.environ, "LC_ALL": "C"}
    env.pop("SLURM_CLUSTERS", None)
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=30,
                                env=env)
    except FileNotFoundError as exc:
        raise ValueError("Missing SSH or Slurm client. Run on a login node or configure DAEMON_SLURM_HOST.") from exc
    if result.returncode:
        raise RuntimeError(f"{argv[0]} failed: {result.stderr.strip()[:400]}")
    return result.stdout


def gpu_tres(text):
    values = {}
    for item in text.split(","):
        key, _, value = item.partition("=")
        if key == "gres/gpu" or key.startswith("gres/gpu:"):
            values[key] = int(value)
    return values


def gpu_gres(text):
    # Commas inside IDX and socket ranges belong to the same GRES.
    result = {}
    for token in re.split(r",(?![^()]*\))", text):
        if not token.startswith("gpu:"):
            continue
        match = re.fullmatch(r"gpu:(?:(?P<model>[^:()]+):)?(?P<count>\d+)(?:\((?P<detail>[^()]*)\))?", token)
        if not match:
            raise ValueError(f"Unrecognized GPU GRES: {token}")
        model = match["model"] or "untyped"
        count, details = result.get(model, (0, []))
        result[model] = count + int(match["count"]), details + [match["detail"] or ""]
    return result


def indices(details, total, used):
    if not details or not all(d.startswith("IDX:") for d in details):
        return None
    if all(d == "IDX:N/A" for d in details):
        return None
    occupied = set()
    for detail in details:
        for item in detail[4:].split(","):
            if item == "N/A":
                continue
            match = re.fullmatch(r"(\d+)(?:-(\d+))?", item)
            if not match:
                return None
            low, high = int(match[1]), int(match[2] or match[1])
            if not 0 <= low <= high < total:
                return None
            occupied.update(range(low, high + 1))
    return sorted(occupied) if len(occupied) == used else None


def parse_nodes(text, partitions):
    nodes = []
    seen = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = dict(re.findall(r"(?:^|\s)(\w+)=([^\s]*)", line))
        name = fields.get("NodeName")
        if not name or name in seen:
            raise ValueError("Missing or duplicate Slurm NodeName in controller response")
        seen.add(name)
        cfg = gpu_tres(fields.get("CfgTRES", ""))
        configured = gpu_gres(fields.get("Gres", ""))
        if not configured:
            configured = {k[9:]: (v, []) for k, v in cfg.items() if k.startswith("gres/gpu:")}
        if not configured and cfg.get("gres/gpu"):
            configured = {"untyped": (cfg["gres/gpu"], [])}
        if not configured:
            continue
        total = sum(count for count, _ in configured.values())
        if "gres/gpu" in cfg and cfg["gres/gpu"] != total:
            raise ValueError(f"Inconsistent configured GPU count on {name}")
        used = gpu_gres(fields.get("GresUsed", ""))
        alloc = gpu_tres(fields.get("AllocTRES", ""))
        used_sum = sum(n for n, _ in used.values())
        conflicting_usage = bool(used and "gres/gpu" in alloc and used_sum != alloc["gres/gpu"])
        groups = sorted(set(fields.get("Partitions", "").split(",")) - {"", "(null)"})
        up = [p for p in groups if partitions.get(p) == "up"]
        state = fields.get("State", "UNKNOWN")
        reasons = []
        if state not in {"IDLE", "MIXED"}:
            reasons.append(state)
        if not up:
            reasons.append("无已启用分区")
        if fields.get("GresDrain", "") not in {"N/A", "(null)", "None"}:
            reasons.append("GPU 排空状态未知或存在排空资源")
        if re.search(r"(?:^|,)(?:mps|shard):", fields.get("Gres", "")):
            reasons.append("共享 GRES，整卡可用性待确认")
        cpu_total = fields.get("CPUEfctv", fields.get("CPUTot", ""))
        if cpu_total.isdigit() and fields.get("CPUAlloc", "").isdigit() and int(fields["CPUAlloc"]) >= int(cpu_total):
            reasons.append("无可分配 CPU")
        if fields.get("RealMemory", "").isdigit() and fields.get("AllocMem", "").isdigit() and int(fields["AllocMem"]) >= int(fields["RealMemory"]):
            reasons.append("无可分配内存")
        for model, (count, _) in configured.items():
            entry = used.get(model)
            if len(configured) == 1 and entry is None and set(used) == {"untyped"}:
                entry = used["untyped"]
            accounted = alloc.get("gres/gpu:" + model)
            if len(configured) == 1 and accounted is None:
                accounted = alloc.get("gres/gpu")
            occupied = entry[0] if entry else accounted
            has_allocation_data = "=" in fields.get("AllocTRES", "")
            if occupied is None and not used_sum and not alloc and has_allocation_data and "gres/gpu" in cfg:
                occupied = 0
            if conflicting_usage or occupied is not None and (
                not 0 <= occupied <= count or accounted is not None and occupied != accounted
            ):
                occupied = None
            ids = indices(entry[1], count, occupied) if entry and len(configured) == 1 else None
            free = None if occupied is None else count - occupied
            row = dict(node=name, model=model, total=count, allocated=occupied, unallocated=free,
                       allocated_ids=ids, free_ids=None if ids is None else sorted(set(range(count)) - set(ids)),
                       state=state, partitions=groups, up_partitions=up, blocked=reasons,
                       candidate=bool(free and not reasons))
            nodes.append(row)
    if not seen:
        raise ValueError("Slurm returned no node records")
    return sorted(nodes, key=lambda row: (row["model"], row["node"]))


def inventory(host=None, socket=None):
    host = host or os.getenv("DAEMON_SLURM_HOST")
    socket = socket or os.getenv("DAEMON_SLURM_SOCKET")
    status = query(["sinfo", "--local", "-a", "-h", "-o", "%R|%a"], host, socket)
    partitions = dict(line.strip().split("|", 1) for line in status.splitlines() if line.strip())
    raw = query(["scontrol", "-a", "-o", "-d", "show", "nodes"], host, socket)
    rows = parse_nodes(raw, partitions)
    return {"observed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "source": host or "local Slurm", "rows": rows}


def select(snapshot, *, free_only=False, partition=None, model=None):
    rows = [r for r in snapshot["rows"] if (not partition or partition in r["partitions"])
            and (not model or model.casefold() == r["model"].casefold())]
    available = [r for r in rows if r["candidate"] and (not partition or partition in r["up_partitions"])]
    models = {}
    for row in rows:
        counts = models.setdefault(row["model"], {"total": 0, "known_unallocated": 0, "candidate_gpus": 0})
        counts["total"] += row["total"]
        counts["known_unallocated"] += row["unallocated"] or 0
    for row in available:
        models[row["model"]]["candidate_gpus"] += row["unallocated"]
    return {"observed_at": snapshot["observed_at"], "source": snapshot["source"], "models": models,
            "total": sum(r["total"] for r in rows),
            "known_unallocated": sum(r["unallocated"] or 0 for r in rows),
            "unknown_capacity": sum(r["total"] for r in rows if r["unallocated"] is None),
            "candidate_gpus": sum(r["unallocated"] for r in available),
            "rows": available if free_only else rows}


def render(report):
    lines = [f'{report["source"]} · {report["observed_at"]}',
             f'配置 {report["total"]} 张，已知未分配 {report["known_unallocated"]} 张，候选 {report["candidate_gpus"]} 张，占用未知的容量 {report["unknown_capacity"]} 张',
             "型号（总数/候选） " + "，".join(f"{k} {v['total']}/{v['candidate_gpus']}" for k,v in report["models"].items()),
             '节点  型号  总数/已分配/未分配  未分配 Slurm IDX  状态  分区']
    for r in report["rows"]:
        ids = "未知" if r["free_ids"] is None else ",".join(map(str, r["free_ids"])) or "—"
        usage = "/".join("?" if r[k] is None else str(r[k]) for k in ("total", "allocated", "unallocated"))
        lines.append(f'{r["node"]}  {r["model"]}  {usage}  {ids}  {r["state"]}  {",".join(r["partitions"])}'
                     + ("  [" + ", ".join(r["blocked"]) + "]" if r["blocked"] else ""))
    if not report["rows"]:
        lines.append("没有符合条件的记录")
    return "\n".join(lines)
