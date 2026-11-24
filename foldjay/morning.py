"""Compare completed runs for the same seed and evaluation scene."""
import hashlib
import json
import math
import stat
from pathlib import Path


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 字段重复 {key!r}")
        result[key] = value
    return result


def _record_with_digest(policy, path):
    file = policy.path(str(path))
    if not stat.S_ISREG(file.stat().st_mode):
        raise ValueError(f"{path} 应为普通文件")
    with file.open("rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError(f"{path} 超过 64 KiB")
    try:
        record = json.loads(data.decode("utf-8"), object_pairs_hook=unique_fields)
    except RecursionError as exc:
        raise ValueError(f"{path} JSON 嵌套层数过多") from exc
    if not isinstance(record, dict):
        raise ValueError(f"{path} 应为 JSON 对象")
    return record, hashlib.sha256(data).hexdigest()


def read_record(policy, path):
    record, _ = _record_with_digest(policy, path)
    return record


def inspect_run(policy, directory, group, seed, scene):
    row = {"group": group, "seed": seed, "issues": [], "sha256": {}}
    identifiers = {}
    score = None
    for filename, kind in (("job.json", "job"), ("eval.json", "evaluation")):
        source = directory / filename
        row[kind] = str(source)
        try:
            data, digest = _record_with_digest(policy, source)
            row["sha256"][kind] = digest
            if data.get("group") != group or type(data.get("seed")) is not int or data["seed"] != seed or data.get("scene") != scene:
                raise ValueError("组、seed 或场景与计划不符")
            if "run_id" in data:
                run_id = data["run_id"]
                if type(run_id) is not int and not (isinstance(run_id, str) and run_id.strip()):
                    raise ValueError("run_id 应为非空字符串或整数")
                identifiers[kind] = run_id
            if kind == "job":
                if data.get("status") != "COMPLETED":
                    raise ValueError(f"作业状态 {data.get('status')!r}")
            else:
                score = data.get("score")
                if type(score) is not int and not (type(score) is float and math.isfinite(score)):
                    raise ValueError("score 应为有限数值")
        except FileNotFoundError:
            row["issues"].append(f"缺少 {source}")
        except (ValueError, OSError) as exc:
            row["issues"].append(f"{source} 无法采信（{exc}）")
    if not row["issues"] and identifiers.get("job") != identifiers.get("evaluation"):
        row["issues"].append(f"{directory} 作业与评测的 run_id 不匹配")
    return row, score


def validate_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError("实验计划应为 JSON 对象")
    groups, seeds, scene = (plan.get(key) for key in ("groups", "seeds", "scene"))
    if not isinstance(groups, list) or len(groups) != 2 or any(
        not isinstance(g, str) or not g or g in {".", ".."} or Path(g).name != g for g in groups
    ) or groups[0] == groups[1]:
        raise ValueError("groups 需要两个不同的组目录名")
    if not isinstance(seeds, list) or not seeds or any(type(s) is not int for s in seeds) or len(set(seeds)) != len(seeds):
        raise ValueError("seeds 需要一组不重复的整数")
    if not isinstance(scene, str) or not scene.strip():
        raise ValueError("需要提供评测场景 scene")

    return groups, seeds, scene


def morning_report(policy, path):
    root = policy.path(path).relative_to(policy.workspace)
    manifest = root / "experiment.json"
    plan, manifest_sha256 = _record_with_digest(policy, manifest)
    groups, seeds, scene = validate_plan(plan)
    rows, pairs = [], []
    for seed in seeds:
        scores = {}
        for group in groups:
            row, score = inspect_run(policy, root / group / str(seed), group, seed, scene)
            rows.append(row)
            if not row["issues"]:
                scores[group] = score
        if len(scores) == len(groups):
            pairs.append({"seed": seed, "scores": scores})
    return {"manifest": str(manifest), "manifest_sha256": manifest_sha256,
            "scene": scene, "seeds": seeds, "runs": rows, "pairs": pairs}


def render_report(report):
    lines = [f"实验晨报 · {report['scene']}", f"计划文件 {report['manifest']}",
             f"可配对 {len(report['pairs'])}/{len(report['seeds'])} 个 seed"]
    for pair in report["pairs"]:
        scores = "，".join(f"{group}={score}" for group, score in pair["scores"].items())
        lines.append(f"  seed {pair['seed']}  {scores}")
    lines.append("逐项依据")
    for row in report["runs"]:
        evidence = [row["job"], row["evaluation"]]
        details = "；".join(row["issues"]) if row["issues"] else "核验通过"
        lines.append(f"  {row['group']} / seed {row['seed']}  {details}  [{' | '.join(evidence)}]")
    return "\n".join("".join(c if c.isprintable() else ascii(c)[1:-1] for c in line) for line in lines)
