#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MAX_CONCURRENCY = 15
DEFAULT_RETRIES = 3
RETRYABLE_ERROR_KINDS = {"429", "timeout", "transient_5xx"}
FATAL_ERROR_KINDS = {"permission", "not_found", "invalid_evidence", "metadata_mismatch", "other"}
SPECIAL_ERROR_KINDS = {"layout_mismatch"}
INCOMPLETE_DOCUMENT_STATUSES = {"pending", "failed", "deferred_layout", "pending_retry", "preflight_pending", "preflight_retry", "needs_discovery", "in_progress"}
WORKFLOW_VERSION = 2
WORKFLOW_COMMANDS = ("capabilities", "workflow", "specs", "plan", "ingest", "ingest-batch", "fail", "status")
INTERNAL_ERROR_LIMIT = 3
RUNTIME_DIR = Path(__file__).resolve().parent
if (RUNTIME_DIR.parent / "references" / "exam_profiles.json").is_file():
    PROFILE_CONFIG_PATH = RUNTIME_DIR.parent / "references" / "exam_profiles.json"
elif (RUNTIME_DIR / "exam_profiles.json").is_file():
    PROFILE_CONFIG_PATH = RUNTIME_DIR / "exam_profiles.json"
else:
    PROFILE_CONFIG_PATH = RUNTIME_DIR.parent / "references" / "exam_profiles.json"


class ManifestError(RuntimeError):
    pass


class ManifestCliError(ManifestError):
    def __init__(self, message: str, *, invalid_command: str = ""):
        super().__init__(message)
        self.invalid_command = invalid_command


class LayoutMismatch(ManifestError):
    pass


class ManifestArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        match = re.search(r"invalid choice: ['\"]([^'\"]+)", message)
        raise ManifestCliError(f"命令参数错误：{message}", invalid_command=match.group(1) if match else "")


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def capabilities_payload() -> dict[str, Any]:
    return {
        "schema_version": WORKFLOW_VERSION,
        "workflow_version": WORKFLOW_VERSION,
        "commands": list(WORKFLOW_COMMANDS),
        "command_schemas": {
            "capabilities": {"required": [], "optional": ["--json"]},
            "workflow": {
                "required": ["--manifest <path>"],
                "optional": [
                    "--response <path>",
                    "--evidence-dir <path>",
                    "--result-json <path>",
                    "--operation-id <id>",
                    "--refresh",
                ],
                "constraints": [
                    "--evidence-dir and --result-json are mutually exclusive",
                    "--result-json requires the current score operation_id",
                ],
            },
            "specs": {"compatibility_only": True, "required": ["--manifest <path>"], "optional": ["--stage initial|students|all"]},
            "plan": {"compatibility_only": True, "required": ["--manifest <path>"], "optional": ["--stage initial|students|all", "--revisions <path>", "--refresh"]},
            "ingest": {"compatibility_only": True, "required": ["--manifest <path>", "--item-id <id>", "--evidence <path>"]},
            "ingest-batch": {"compatibility_only": True, "required": ["--manifest <path>", "--evidence-dir <path>"]},
            "fail": {"compatibility_only": True, "required": ["--manifest <path>", "--item-id <id>", "--error <text>"], "optional": ["--error-kind <kind>"]},
            "status": {"compatibility_only": True, "required": ["--manifest <path>"]},
        },
        "recommended_command": "workflow",
        "recommended_invocation": "python3 scripts/manifest_runtime.py workflow --manifest /absolute/path/to/task.json",
        "workflow_statuses": ["action_required", "retrying", "ready_to_score", "awaiting_user", "complete", "engineering_blocked"],
        "action_types": ["preflight_docs", "read_docs", "score"],
        "accepted_preflight_shapes": [
            "workflow-v2 envelope with items",
            "object with items array",
            "items array",
            "item_id to revision mapping",
            "unique document_id to revision mapping",
        ],
        "error_policy": {
            "contract_errors": "recoverable and do not consume Docs retries",
            "external_retries": DEFAULT_RETRIES,
            "formal_outputs_require_complete_evidence": True,
        },
    }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def canonical_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop volatile local/read metadata while preserving scoring and source identity."""
    return {
        key: value
        for key, value in payload.items()
        if key != "read_at" and not key.startswith("_") and key not in {"content_sha256", "evidence_sha256"}
    }


def evidence_content_hash(payload: dict[str, Any]) -> str:
    return stable_hash(canonical_evidence(payload))


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix=path.name + ".", suffix=".tmp", dir=path.parent, delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def resolve_path(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def document_id_from_url(url: str) -> str:
    if not str(url or "").strip():
        return ""
    path = urlparse(url).path.rstrip("/")
    candidate = path.rsplit("/", 1)[-1] if path else ""
    return candidate or stable_hash(url)[:16]


def normalize_header(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def load_profile_config() -> dict[str, Any]:
    try:
        payload = json.loads(PROFILE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"无法读取考试配置：{PROFILE_CONFIG_PATH}：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), dict):
        raise ManifestError("考试配置缺少 profiles")
    return payload


def classify_error_kind(error: str, explicit: str | None = None) -> str:
    if explicit:
        if explicit not in RETRYABLE_ERROR_KINDS | FATAL_ERROR_KINDS | SPECIAL_ERROR_KINDS:
            raise ManifestError(f"未知错误类型：{explicit}")
        return explicit
    lowered = str(error).lower()
    if any(token in lowered for token in ("permission", "forbidden", "权限", "无权", "403")):
        return "permission"
    if any(token in lowered for token in ("not found", "不存在", "404")):
        return "not_found"
    if "429" in lowered or "rate limit" in lowered or "限流" in lowered:
        return "429"
    if any(token in lowered for token in ("timeout", "timed out", "超时")):
        return "timeout"
    if re.search(r"\b5\d\d\b", lowered) or "server error" in lowered:
        return "transient_5xx"
    return "other"


def normalize_document_spec(raw: dict[str, Any], *, role: str, student: str = "", defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ManifestError(f"{role} 文档配置必须是对象")
    merged = {**(defaults or {}), **raw}
    document = merged.get("document") if isinstance(merged.get("document"), dict) else {}
    url = str(merged.get("url") or document.get("url") or "")
    document_id = str(merged.get("id") or document.get("id") or (document_id_from_url(url) if url else ""))
    if not url and not document_id:
        raise ManifestError(f"{role} 文档必须提供 url 或 id")
    item_seed = {"role": role, "student": student, "id": document_id, "url": url}
    return {
        "item_id": str(merged.get("item_id") or stable_hash(item_seed)[:20]),
        "role": role,
        "student": student,
        "url": url,
        "document_id": document_id,
        "revision": str(merged.get("revision") or document.get("revision") or ""),
        "sheet": str(merged.get("sheet") or ""),
        "range": str(merged.get("range") or ""),
        "evidence": str(merged.get("evidence") or ""),
    }


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"无法读取Manifest：{path}：{exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ManifestError("Manifest必须是 schema_version=1 的JSON对象")
    required = ["task_id", "exam_profile", "group", "progress", "standard", "homework"]
    missing = [key for key in required if not manifest.get(key)]
    if missing:
        raise ManifestError("Manifest缺少字段：" + ", ".join(missing))
    runtime = manifest.setdefault("runtime", {})
    concurrency = int(runtime.get("max_concurrency", MAX_CONCURRENCY))
    if concurrency < 1 or concurrency > MAX_CONCURRENCY:
        raise ManifestError(f"max_concurrency 必须在 1–{MAX_CONCURRENCY} 之间")
    retries = int(runtime.get("retries", DEFAULT_RETRIES))
    if retries < 1:
        raise ManifestError("retries 必须至少为1")
    runtime["max_concurrency"] = concurrency
    runtime["retries"] = retries
    runtime.setdefault("retry_delays_seconds", [1, 2])
    delays = runtime["retry_delays_seconds"]
    if not isinstance(delays, list) or len(delays) < max(0, retries - 1) or any(not isinstance(value, (int, float)) or value < 0 for value in delays):
        raise ManifestError("retry_delays_seconds 必须是长度不小于 retries-1 的非负数数组")
    runtime.setdefault("learn_homework_layout", True)
    runtime.setdefault("ocr_engine", "auto")
    runtime.setdefault("ocr_workers", 4)
    runtime.setdefault("ocr_confidence_threshold", 0.75)
    if not 1 <= int(runtime["ocr_workers"]) <= 8:
        raise ManifestError("ocr_workers 必须在 1–8 之间")
    if not 0 <= float(runtime["ocr_confidence_threshold"]) <= 1:
        raise ManifestError("ocr_confidence_threshold 必须在 0–1 之间")
    if runtime["ocr_engine"] not in {"auto", "vision", "api"}:
        raise ManifestError("ocr_engine 必须是 auto、vision 或 api")
    output = manifest.setdefault("output", {})
    output.setdefault("dir", "output")
    output.setdefault("png", "off")
    output.setdefault("xlsx", "off")
    if output["png"] not in {"on", "off"}:
        raise ManifestError("Manifest output.png 必须为 on 或 off")
    if output["xlsx"] not in {"auto", "on", "off"}:
        raise ManifestError("Manifest output.xlsx 必须为 auto、on 或 off")
    manifest["_path"] = str(path.resolve())
    manifest["_base"] = str(path.resolve().parent)
    return manifest


def cache_root(manifest: dict[str, Any]) -> Path:
    base = Path(manifest["_base"])
    configured = manifest.get("cache", {}).get("dir", ".score-cache") if isinstance(manifest.get("cache"), dict) else ".score-cache"
    return resolve_path(base, str(configured)) or (base / ".score-cache")


def state_path(manifest: dict[str, Any]) -> Path:
    safe_task = re.sub(r"[^0-9A-Za-z._-]+", "_", str(manifest["task_id"])).strip("._") or "task"
    task_digest = stable_hash(str(manifest["task_id"]))[:10]
    return cache_root(manifest) / "runs" / f"{safe_task[:80]}--{task_digest}.json"


def legacy_state_path(manifest: dict[str, Any]) -> Path:
    safe_task = re.sub(r"[^0-9A-Za-z._-]+", "_", str(manifest["task_id"])).strip("._") or "task"
    return cache_root(manifest) / "runs" / f"{safe_task}.json"


@contextmanager
def state_lock(manifest: dict[str, Any]):
    """Small cross-platform lock around read/modify/write state transactions."""
    lock_path = state_path(manifest).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 30
    descriptor: int | None = None
    owner_token = secrets.token_hex(16)

    def process_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def lock_owner() -> tuple[int, str]:
        try:
            text = lock_path.read_text(encoding="utf-8")
            payload = json.loads(text)
            if not isinstance(payload, dict):
                return 0, ""
            return int(payload.get("pid", 0)), str(payload.get("token") or "")
        except json.JSONDecodeError:
            try:
                return int(text.splitlines()[0]), ""
            except (ValueError, IndexError):
                return 0, ""
        except (OSError, ValueError, TypeError):
            return 0, ""

    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, json.dumps({"pid": os.getpid(), "token": owner_token, "created_at": time.time()}).encode("utf-8"))
        except FileExistsError:
            try:
                pid, _ = lock_owner()
                if time.time() - lock_path.stat().st_mtime > 120 and not process_alive(pid):
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise ManifestError(f"任务状态正被其他进程占用：{lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            _, current_token = lock_owner()
            if current_token == owner_token:
                lock_path.unlink(missing_ok=True)


def load_state(manifest: dict[str, Any]) -> dict[str, Any]:
    path = state_path(manifest)
    if not path.exists():
        legacy = legacy_state_path(manifest)
        if legacy.exists() and legacy != path:
            path = legacy
    if not path.exists():
        return {
            "schema_version": 2,
            "task_id": manifest["task_id"],
            "documents": {},
            "resolved_students": [],
            "layouts": {},
            "scheduler": {
                "effective_concurrency": manifest["runtime"]["max_concurrency"],
                "success_batch_streak": 0,
                "max_observed_batch": 0,
                "batches": {},
                "next_batch_number": 1,
            },
            "timings": {},
            "workflow": {
                "version": WORKFLOW_VERSION,
                "revision": 0,
                "current_operation": {},
                "applied_operations": {},
                "preflight": {},
                "contract_errors": {},
                "recoveries": [],
                "terminal": {},
            },
            "index_issues": [],
        }
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"任务状态损坏：{path}：{exc}") from exc
    if str(state.get("task_id") or "") != str(manifest["task_id"]):
        raise ManifestError(f"任务状态 task_id 不匹配：{path}")
    state.setdefault("documents", {})
    state.setdefault("resolved_students", [])
    state.setdefault("layouts", {})
    state.setdefault("schema_version", 2)
    scheduler = state.setdefault("scheduler", {})
    scheduler.setdefault("effective_concurrency", manifest["runtime"]["max_concurrency"])
    scheduler["effective_concurrency"] = min(int(scheduler["effective_concurrency"]), manifest["runtime"]["max_concurrency"], MAX_CONCURRENCY)
    scheduler.setdefault("success_batch_streak", 0)
    scheduler.setdefault("max_observed_batch", 0)
    scheduler.setdefault("batches", {})
    scheduler.setdefault("next_batch_number", 1)
    state.setdefault("timings", {})
    workflow = state.setdefault("workflow", {})
    workflow.setdefault("version", WORKFLOW_VERSION)
    workflow.setdefault("revision", 0)
    workflow.setdefault("current_operation", {})
    workflow.setdefault("applied_operations", {})
    workflow.setdefault("preflight", {})
    workflow.setdefault("contract_errors", {})
    workflow.setdefault("recoveries", [])
    workflow.setdefault("terminal", {})
    state.setdefault("index_issues", [])
    return state


def save_state(manifest: dict[str, Any], state: dict[str, Any]) -> None:
    atomic_write_json(state_path(manifest), state)


def evidence_cache_key(spec: dict[str, Any]) -> str | None:
    if not spec.get("revision"):
        return None
    return stable_hash({key: spec.get(key, "") for key in ("role", "student", "document_id", "revision", "sheet", "range")})


def evidence_cache_path(manifest: dict[str, Any], spec: dict[str, Any]) -> Path | None:
    key = evidence_cache_key(spec)
    return cache_root(manifest) / "evidence" / f"{key}.json" if key else None


def legacy_evidence_cache_path(manifest: dict[str, Any], spec: dict[str, Any]) -> Path | None:
    if not spec.get("revision"):
        return None
    key = stable_hash({key: spec.get(key, "") for key in ("document_id", "revision", "sheet", "range")})
    return cache_root(manifest) / "evidence" / f"{key}.json"


def document_specs(manifest: dict[str, Any], state: dict[str, Any], *, stage: str = "all") -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []

    def reuse_state(spec: dict[str, Any]) -> dict[str, Any]:
        stored = state.get("documents", {}).get(spec["item_id"], {})
        clone = dict(spec)
        for key in ("revision", "sheet", "range"):
            if not clone.get(key) and stored.get(key):
                clone[key] = stored[key]
        return clone

    if stage in {"initial", "all"}:
        specs.append(reuse_state(normalize_document_spec(manifest["standard"], role="standard")))
        homework = manifest["homework"]
        if isinstance(homework, dict) and homework.get("index"):
            specs.append(reuse_state(normalize_document_spec(homework["index"], role="homework_index")))
    if stage in {"students", "all"}:
        homework = manifest["homework"]
        defaults = homework.get("document_defaults", {}) if isinstance(homework, dict) else {}
        explicit = homework.get("documents", []) if isinstance(homework, dict) else []
        rows = explicit or state.get("resolved_students", [])
        for item in rows:
            if not isinstance(item, dict):
                raise ManifestError("homework.documents 每项必须是对象")
            student = str(item.get("student") or item.get("name") or "").strip()
            if not student:
                raise ManifestError("学员作业配置缺少 student")
            specs.append(reuse_state(normalize_document_spec(item, role="homework", student=student, defaults=defaults)))
    return specs


def homework_layout_config(manifest: dict[str, Any]) -> dict[str, Any]:
    homework = manifest.get("homework") if isinstance(manifest.get("homework"), dict) else {}
    configured = homework.get("layout_reuse") if isinstance(homework.get("layout_reuse"), dict) else {}
    return {
        "enabled": bool(configured.get("enabled", manifest["runtime"].get("learn_homework_layout", True))),
        "force_probe": bool(configured.get("enabled", False)),
        "discovery_range": str(configured.get("discovery_range") or ""),
    }


def prepare_homework_layout_specs(
    manifest: dict[str, Any],
    state: dict[str, Any],
    specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    config = homework_layout_config(manifest)
    if not config["enabled"]:
        return specs
    learned = state.get("layouts", {}).get("homework")
    prepared: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for original in specs:
        spec = dict(original)
        if spec.get("role") != "homework":
            prepared.append(spec)
            continue
        if spec.get("workflow_preflight_pending"):
            prepared.append(spec)
            continue
        if spec.get("preflight_error"):
            prepared.append(spec)
            continue
        entry = state.get("documents", {}).get(spec["item_id"], {})
        if entry.get("status") == "failed" and entry.get("error_kind") in FATAL_ERROR_KINDS and not spec.get("preflight_ok"):
            prepared.append(spec)
            continue
        if entry.get("status") == "needs_discovery":
            spec.update({"sheet": "", "range": "", "read_mode": "discovery_fallback", "discovery_range": config["discovery_range"]})
            prepared.append(spec)
            continue
        if entry.get("read_mode") == "discovery_fallback" and entry.get("status") in {"success", "cached"} and spec.get("sheet") and spec.get("range"):
            spec["read_mode"] = "fixed_exception"
            prepared.append(spec)
            continue
        if learned:
            spec["sheet"] = str(learned.get("sheet") or "")
            spec["range"] = str(learned.get("range") or "")
            spec.update({"read_mode": "learned_fast", "layout_key": "homework"})
            prepared.append(spec)
            continue
        if config["force_probe"]:
            unresolved.append(spec)
            continue
        if spec.get("sheet") and spec.get("range"):
            spec.setdefault("read_mode", "fixed")
            prepared.append(spec)
            continue
        unresolved.append(spec)

    if not unresolved:
        return prepared
    probe_item_id = str(state.get("scheduler", {}).get("layout_probe_item_id") or "")
    if not any(item["item_id"] == probe_item_id for item in unresolved):
        probe_item_id = unresolved[0]["item_id"]
        state.setdefault("scheduler", {})["layout_probe_item_id"] = probe_item_id
    for spec in unresolved:
        if spec["item_id"] == probe_item_id:
            spec.update({"range": "", "read_mode": "discovery_probe", "discovery_range": config["discovery_range"]})
        else:
            spec.update({"read_mode": "deferred_layout", "deferred_layout": True})
        prepared.append(spec)
    return prepared


def resolve_unique_header(headers: list[Any], candidates: list[str], label: str) -> str:
    candidate_set = {normalize_header(value) for value in candidates}
    matches = [normalize_header(value) for value in headers if normalize_header(value) in candidate_set]
    if len(matches) != 1:
        raise LayoutMismatch(f"快速范围的{label}表头匹配数为{len(matches)}，预期为1")
    return matches[0]


def inspect_homework_layout(manifest: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    config = load_profile_config()
    profile = config["profiles"].get(manifest["exam_profile"])
    if not isinstance(profile, dict):
        raise ManifestError(f"未知考试配置：{manifest['exam_profile']}")
    headers = payload.get("headers")
    rows = payload.get("rows")
    if not isinstance(headers, list) or not isinstance(rows, list):
        raise LayoutMismatch("快速范围证据缺少 headers 或 rows 数组")
    id_header = resolve_unique_header(headers, list(config.get("id_headers") or []), "ID")
    dimension_headers: dict[str, str] = {}
    for dimension in profile.get("dimensions", []):
        aliases = list(config.get("header_aliases", {}).get(dimension) or [dimension])
        dimension_headers[dimension] = resolve_unique_header(headers, aliases, dimension)
    id_index = next(index for index, value in enumerate(headers) if normalize_header(value) == id_header)
    ids = [str(row[id_index]).strip() for row in rows if isinstance(row, list) and id_index < len(row) and str(row[id_index]).strip()]
    if not ids:
        raise LayoutMismatch("快速范围内没有有效ID")
    if len(ids) != len(set(ids)):
        raise LayoutMismatch("快速范围内ID重复")
    sheet = str(payload.get("sheet") or "")
    cell_range = str(payload.get("range") or "")
    if not sheet or not cell_range:
        raise LayoutMismatch("快速范围证据缺少页签或范围")
    return {
        "sheet": sheet,
        "range": cell_range,
        "id_header": id_header,
        "dimension_headers": dimension_headers,
        "id_count": len(ids),
        "header_sha256": stable_hash([normalize_header(value) for value in headers]),
    }


def validate_learned_layout(manifest: dict[str, Any], payload: dict[str, Any], learned: dict[str, Any]) -> None:
    observed = inspect_homework_layout(manifest, payload)
    if observed["id_count"] != int(learned.get("id_count", 0)):
        raise LayoutMismatch(f"快速范围有效ID数={observed['id_count']}，首份模板={learned.get('id_count')}")


def merge_revisions(specs: list[dict[str, Any]], revisions_path: Path | None) -> list[dict[str, Any]]:
    if revisions_path is None:
        return specs
    payload = json.loads(revisions_path.read_text(encoding="utf-8"))
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ManifestError("revision快照必须是数组或含items数组的对象")
    if not all(isinstance(item, dict) for item in items):
        raise ManifestError("revision快照每项必须是对象")
    item_ids = [str(item.get("item_id") or "") for item in items]
    if any(not item_id for item_id in item_ids):
        raise ManifestError("revision快照存在空 item_id")
    if len(item_ids) != len(set(item_ids)):
        raise ManifestError("revision快照存在重复 item_id")
    expected_ids = {str(spec["item_id"]) for spec in specs}
    observed_ids = set(item_ids)
    unknown = sorted(observed_ids - expected_ids)
    missing = sorted(expected_ids - observed_ids)
    if unknown or missing:
        parts = []
        if unknown:
            parts.append("未知=" + ",".join(unknown))
        if missing:
            parts.append("缺失=" + ",".join(missing))
        raise ManifestError("revision快照必须完整且仅包含本次读取项：" + "；".join(parts))
    by_id = {str(item["item_id"]): item for item in items}
    merged: list[dict[str, Any]] = []
    for spec in specs:
        update = by_id.get(spec["item_id"], {})
        clone = dict(spec)
        if update:
            clone["preflight_checked"] = True
        for key in ("revision", "sheet", "range"):
            if update.get(key) is not None:
                clone[key] = str(update[key])
        if update.get("error") or update.get("error_kind"):
            clone["preflight_error"] = str(update.get("error") or "预检失败")
            clone["preflight_error_kind"] = classify_error_kind(clone["preflight_error"], str(update.get("error_kind") or "") or None)
        elif update:
            clone["preflight_ok"] = True
        merged.append(clone)
    return merged


def find_cached_evidence(manifest: dict[str, Any], spec: dict[str, Any], state: dict[str, Any], *, refresh: bool = False) -> Path | None:
    if refresh or not spec.get("revision"):
        return None
    path = evidence_cache_path(manifest, spec)
    candidates = [path, legacy_evidence_cache_path(manifest, spec)]
    for candidate in candidates:
        if candidate and candidate.exists():
            try:
                validate_evidence_metadata(spec, load_evidence_payload(candidate))
            except (OSError, json.JSONDecodeError, ManifestError):
                continue
            return candidate
    entry = state.get("documents", {}).get(spec["item_id"], {})
    cached = Path(entry.get("cache_path", "")) if entry.get("cache_path") else None
    if cached and cached.exists() and all(str(entry.get(key, "")) == str(spec.get(key, "")) for key in ("role", "student", "document_id", "revision", "sheet", "range")):
        try:
            validate_evidence_metadata(spec, load_evidence_payload(cached))
        except (OSError, json.JSONDecodeError, ManifestError):
            return None
        return cached
    return None


def _plan_reads_unlocked(manifest: dict[str, Any], specs: list[dict[str, Any]], *, refresh: bool = False) -> dict[str, Any]:
    state = load_state(manifest)
    homework = manifest.get("homework") if isinstance(manifest.get("homework"), dict) else {}
    if "documents" in homework and (any(spec.get("role") == "homework" for spec in specs) or not homework.get("documents")):
        active_ids = {spec["item_id"] for spec in specs if spec.get("role") == "homework"}
        for stored_id, stored in state.get("documents", {}).items():
            if stored.get("role") == "homework" and stored_id not in active_ids:
                stored.update({"status": "removed", "error": "已从显式作业清单移除"})
    specs = prepare_homework_layout_specs(manifest, state, specs)
    cached: list[dict[str, Any]] = []
    reads: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    retry: list[dict[str, Any]] = []
    for spec in specs:
        entry = state["documents"].setdefault(spec["item_id"], {})
        entry.update({key: spec.get(key, "") for key in ("role", "student", "url", "document_id", "revision", "sheet", "range", "read_mode")})
        if spec.get("workflow_preflight_pending"):
            if entry.get("status") != "preflight_retry":
                entry.update({"status": "preflight_pending", "error": entry.get("error", "")})
            deferred.append({**spec, "deferred_reason": "awaiting_preflight"})
            continue
        if spec.get("workflow_terminal_failure"):
            failed.append({
                **spec,
                "error": entry.get("error", ""),
                "error_kind": entry.get("error_kind", "other"),
                "attempts": int(entry.get("attempts", 0)),
            })
            continue
        if spec.get("deferred_layout"):
            entry.update({"status": "deferred_layout", "error": "等待本次考核首份作业固化页签和范围"})
            deferred.append(spec)
            continue
        preflight_error = str(spec.get("preflight_error") or "")
        if preflight_error:
            error_kind = classify_error_kind(preflight_error, str(spec.get("preflight_error_kind") or "") or None)
            attempts = int(entry.get("attempts", 0)) + 1
            retryable = error_kind in RETRYABLE_ERROR_KINDS and attempts < int(manifest["runtime"]["retries"])
            next_attempt_at = ""
            if retryable:
                delays = manifest["runtime"]["retry_delays_seconds"]
                delay = float(delays[min(attempts - 1, len(delays) - 1)])
                next_attempt_at = utc_text(utc_now() + timedelta(seconds=delay))
            entry.update({
                "status": "preflight_retry" if retryable else "failed",
                "error": preflight_error,
                "error_kind": error_kind,
                "attempts": attempts,
                "next_attempt_at": next_attempt_at,
                "finished_at": utc_text(),
            })
            outcome = {
                **spec,
                "error": preflight_error,
                "error_kind": error_kind,
                "attempts": attempts,
                "next_attempt_at": next_attempt_at,
            }
            if retryable:
                retry.append(outcome)
            else:
                failed.append(outcome)
            continue
        if entry.get("status") == "failed" and entry.get("error_kind") in FATAL_ERROR_KINDS and not refresh and not spec.get("preflight_ok"):
            failed.append({**spec, "error": entry.get("error", ""), "error_kind": entry.get("error_kind", "other")})
            continue
        if entry.get("status") == "pending_retry" and entry.get("next_attempt_at"):
            try:
                next_attempt = datetime.fromisoformat(str(entry["next_attempt_at"]))
            except ValueError:
                next_attempt = utc_now()
            if next_attempt > utc_now():
                retry.append({**spec, "error": entry.get("error", ""), "error_kind": entry.get("error_kind", ""), "next_attempt_at": entry["next_attempt_at"]})
                continue
        cached_path = find_cached_evidence(manifest, spec, state, refresh=refresh)
        if cached_path:
            entry.update({"status": "cached", "cache_path": str(cached_path), "error": ""})
            if spec.get("role") == "homework_index":
                payload = load_evidence_payload(cached_path)
                resolved_students, index_issues = resolve_index_students_with_issues(payload, manifest)
                state["resolved_students"] = resolved_students
                state["index_issues"] = index_issues
                defaults = homework.get("document_defaults", {})
                active_ids = {
                    normalize_document_spec(item, role="homework", student=str(item["student"]), defaults=defaults)["item_id"]
                    for item in resolved_students
                }
                for stored_id, stored in state.get("documents", {}).items():
                    if stored.get("role") == "homework" and stored_id not in active_ids:
                        stored.update({"status": "removed", "error": "已从作业索引移除"})
            cached.append({**spec, "cache_path": str(cached_path)})
        else:
            entry.update({"status": "pending", "error": ""})
            reads.append(spec)
    save_state(manifest, state)
    return {
        "schema_version": 1,
        "task_id": manifest["task_id"],
        "max_concurrency": manifest["runtime"]["max_concurrency"],
        "retries": manifest["runtime"]["retries"],
        "retry_delays_seconds": manifest["runtime"]["retry_delays_seconds"],
        "cached": cached,
        "read": reads,
        "deferred": deferred,
        "failed": failed,
        "retry": retry,
    }


def plan_reads(manifest: dict[str, Any], specs: list[dict[str, Any]], *, refresh: bool = False) -> dict[str, Any]:
    with state_lock(manifest):
        return _plan_reads_unlocked(manifest, specs, refresh=refresh)


def load_evidence_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ManifestError(f"证据必须是 schema_version=1 的JSON对象：{path}")
    validate_evidence_payload(payload, path)
    return payload


def validate_evidence_payload(payload: dict[str, Any], path: Path | None = None) -> None:
    label = f"：{path}" if path else ""
    source = payload.get("source")
    if source not in {"docs", "image_ocr"}:
        raise ManifestError(f"证据 source 必须是 docs 或 image_ocr{label}")
    document = payload.get("document")
    if not isinstance(document, dict):
        raise ManifestError(f"证据缺少 document 对象{label}")
    if not str(document.get("id") or "").strip():
        raise ManifestError(f"证据 document.id 不能为空{label}")
    headers = payload.get("headers")
    rows = payload.get("rows")
    if not isinstance(headers, list) or not headers:
        raise ManifestError(f"证据 headers 必须是非空数组{label}")
    if not isinstance(rows, list):
        raise ManifestError(f"证据 rows 必须是数组{label}")
    normalized_headers = [normalize_header(value) for value in headers]
    if any(not value for value in normalized_headers):
        raise ManifestError(f"证据表头不能为空{label}")
    if len(normalized_headers) != len(set(normalized_headers)):
        raise ManifestError(f"证据表头重复{label}")
    for row_number, row in enumerate(rows, start=2):
        if not isinstance(row, list):
            raise ManifestError(f"证据第{row_number}行必须是数组{label}")
        if len(row) > len(headers):
            raise ManifestError(f"证据第{row_number}行列数超过表头{label}")


def evidence_metadata(payload: dict[str, Any]) -> dict[str, str]:
    document = payload.get("document") if isinstance(payload.get("document"), dict) else {}
    return {
        "url": str(document.get("url") or ""),
        "document_id": str(document.get("id") or ""),
        "revision": str(document.get("revision") or ""),
        "sheet": str(payload.get("sheet") or ""),
        "range": str(payload.get("range") or ""),
    }


def validate_evidence_metadata(entry: dict[str, Any], payload: dict[str, Any]) -> dict[str, str]:
    if payload.get("source", "docs") != "docs":
        raise ManifestError("Manifest Docs 读取项只能 ingest source=docs 的证据")
    actual = evidence_metadata(payload)
    labels = {"document_id": "文档ID", "revision": "revision", "sheet": "工作表", "range": "范围"}
    identity_mismatches: list[str] = []
    layout_mismatches: list[str] = []
    for key in ("document_id", "revision"):
        planned = str(entry.get(key) or "")
        observed = str(actual.get(key) or "")
        if planned and planned != observed:
            identity_mismatches.append(f"{labels[key]}计划={planned!r}、证据={observed!r}")
    for key in ("sheet", "range"):
        planned = str(entry.get(key) or "")
        observed = str(actual.get(key) or "")
        if planned and planned != observed:
            layout_mismatches.append(f"{labels[key]}计划={planned!r}、证据={observed!r}")
    planned_student = str(entry.get("student") or "").strip()
    evidence_student = str(payload.get("student_name") or "").strip()
    if planned_student and evidence_student and planned_student != evidence_student:
        identity_mismatches.append(f"学员计划={planned_student!r}、证据={evidence_student!r}")
    if planned_student:
        config = load_profile_config()
        headers = list(payload.get("headers") or [])
        candidate_set = {normalize_header(value) for value in config.get("student_name_headers", [])}
        name_indexes = [index for index, value in enumerate(headers) if normalize_header(value) in candidate_set]
        row_identity_found = False
        if len(name_indexes) > 1:
            identity_mismatches.append("证据姓名表头重复")
        elif name_indexes:
            names = {
                str(row[name_indexes[0]]).strip()
                for row in payload.get("rows", [])
                if isinstance(row, list) and name_indexes[0] < len(row) and str(row[name_indexes[0]] or "").strip()
            }
            row_identity_found = bool(names)
            unexpected = sorted(name for name in names if name != planned_student)
            if unexpected:
                identity_mismatches.append(f"学员计划={planned_student!r}、行内姓名={unexpected!r}")
        if not evidence_student and not row_identity_found:
            identity_mismatches.append(f"学员计划={planned_student!r}，但证据没有顶层或行内姓名")
    if identity_mismatches:
        raise ManifestError("证据metadata与读取计划不一致：" + "；".join(identity_mismatches + layout_mismatches))
    if layout_mismatches:
        message = "证据metadata的页签/范围与快速读取计划不一致：" + "；".join(layout_mismatches)
        if entry.get("read_mode") == "learned_fast":
            raise LayoutMismatch(message)
        raise ManifestError(message)
    return actual


def resolve_index_students_with_issues(payload: dict[str, Any], manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    homework = manifest["homework"]
    index_config = homework.get("index", {}) if isinstance(homework, dict) else {}
    headers = payload.get("headers", [])
    rows = payload.get("rows", [])
    normalized = {re.sub(r"\s+", "", str(value)): index for index, value in enumerate(headers)}
    name_candidates = [index_config.get("name_header"), "同学名称", "学员姓名", "姓名"]
    link_candidates = [index_config.get("link_header"), "作业链接", "文档链接", "链接"]
    name_index = next((normalized[re.sub(r"\s+", "", str(value))] for value in name_candidates if value and re.sub(r"\s+", "", str(value)) in normalized), None)
    link_index = next((normalized[re.sub(r"\s+", "", str(value))] for value in link_candidates if value and re.sub(r"\s+", "", str(value)) in normalized), None)
    if name_index is None or link_index is None:
        return [], [{
            "stage": "homework_index",
            "row_number": 1,
            "student": "",
            "error": "作业索引缺少姓名列或作业链接列",
            "next_action": "补全姓名列和作业链接列后复用同一 task_id 续跑",
        }]
    defaults = homework.get("document_defaults", {})
    students_by_name: dict[str, dict[str, Any]] = {}
    duplicate_names: set[str] = set()
    issues: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, start=2):
        if not isinstance(row, list):
            continue
        name = str(row[name_index] if name_index < len(row) else "").strip()
        url = str(row[link_index] if link_index < len(row) else "").strip()
        if not name and not url:
            continue
        if not name or not url:
            issues.append({
                "stage": "homework_index",
                "row_number": row_number,
                "student": name,
                "error": "作业索引该行的姓名或链接为空",
                "next_action": f"补全作业索引第{row_number}行后复用同一 task_id 续跑",
            })
            continue
        if name in students_by_name or name in duplicate_names:
            students_by_name.pop(name, None)
            if name not in duplicate_names:
                issues.append({
                    "stage": "homework_index",
                    "row_number": row_number,
                    "student": name,
                    "error": f"作业索引出现重复学员：{name}",
                    "next_action": "合并或删除重复学员行后复用同一 task_id 续跑",
                })
            duplicate_names.add(name)
            continue
        students_by_name[name] = {"student": name, "url": url, "id": document_id_from_url(url), **defaults}
    return list(students_by_name.values()), issues


def resolve_index_students(payload: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    students, issues = resolve_index_students_with_issues(payload, manifest)
    if issues:
        raise ManifestError("作业索引存在异常：" + "；".join(str(item["error"]) for item in issues))
    return students


def _ingest_evidence_unlocked(manifest: dict[str, Any], item_id: str, evidence_path: Path) -> dict[str, Any]:
    state = load_state(manifest)
    entry = state.get("documents", {}).get(item_id)
    if entry is None:
        raise ManifestError(f"未知读取项：{item_id}；请先执行plan")
    try:
        payload = load_evidence_payload(evidence_path)
        metadata = validate_evidence_metadata(entry, payload)
    except LayoutMismatch as exc:
        entry.update({
            "status": "needs_discovery",
            "sheet": "",
            "range": "",
            "cache_path": "",
            "read_mode": "discovery_fallback",
            "error_kind": "layout_mismatch",
            "error": str(exc),
            "finished_at": utc_text(),
        })
        save_state(manifest, state)
        return {"item_id": item_id, "status": "needs_discovery", "error_kind": "layout_mismatch", "error": str(exc)}
    except (OSError, json.JSONDecodeError, ManifestError) as exc:
        metadata_mismatch = isinstance(exc, ManifestError) and "metadata" in str(exc)
        refresh_attempts = int(entry.get("metadata_refresh_attempts", 0))
        if metadata_mismatch and refresh_attempts < 1:
            entry.update({
                "status": "preflight_pending",
                "revision": "",
                "sheet": "",
                "range": "",
                "cache_path": "",
                "preflight_status": "pending",
                "metadata_refresh_attempts": refresh_attempts + 1,
                "error_kind": "metadata_mismatch",
                "error": str(exc),
                "finished_at": utc_text(),
            })
            save_state(manifest, state)
            return {
                "item_id": item_id,
                "status": "needs_preflight",
                "error_kind": "metadata_mismatch",
                "error": str(exc),
            }
        entry.update({"status": "failed", "error_kind": "metadata_mismatch" if metadata_mismatch else "invalid_evidence", "error": str(exc), "finished_at": utc_text()})
        save_state(manifest, state)
        raise
    learned_layout: dict[str, Any] | None = None
    read_mode = str(entry.get("read_mode") or "")
    if entry.get("role") == "homework" and read_mode in {"discovery_probe", "learned_fast", "discovery_fallback"}:
        try:
            observed_layout = inspect_homework_layout(manifest, payload)
            if read_mode == "learned_fast":
                template = state.get("layouts", {}).get("homework")
                if not isinstance(template, dict):
                    raise LayoutMismatch("本次考核的作业范围模板丢失")
                validate_learned_layout(manifest, payload, template)
            elif read_mode == "discovery_probe":
                learned_layout = {
                    **observed_layout,
                    "source_item_id": item_id,
                    "learned_at": utc_text(),
                    "task_id": manifest["task_id"],
                }
        except LayoutMismatch as exc:
            if read_mode == "learned_fast":
                entry.update({
                    "status": "needs_discovery",
                    "sheet": "",
                    "range": "",
                    "cache_path": "",
                    "read_mode": "discovery_fallback",
                    "error_kind": "layout_mismatch",
                    "error": str(exc),
                    "finished_at": utc_text(),
                })
                save_state(manifest, state)
                return {"item_id": item_id, "status": "needs_discovery", "error_kind": "layout_mismatch", "error": str(exc)}
            entry.update({"status": "failed", "error_kind": "invalid_evidence", "error": str(exc), "finished_at": utc_text()})
            save_state(manifest, state)
            raise
    spec = {**entry, **{key: value for key, value in metadata.items() if value}}
    if not spec.get("revision"):
        content_key = evidence_content_hash(payload)
        cache_path = cache_root(manifest) / "evidence-unrevisioned" / f"{content_key}.json"
    else:
        cache_path = evidence_cache_path(manifest, spec)
        assert cache_path is not None
    atomic_write_json(cache_path, payload)
    entry.update(spec)
    attempts = int(entry.get("attempts", 0)) + (0 if entry.get("status") == "in_progress" else 1)
    entry.update({
        "status": "success",
        "cache_path": str(cache_path),
        "content_sha256": content_key if not spec.get("revision") else evidence_content_hash(payload),
        "error": "",
        "error_kind": "",
        "attempts": attempts,
        "finished_at": utc_text(),
        "next_attempt_at": "",
    })
    if entry.get("role") == "homework_index":
        resolved_students, index_issues = resolve_index_students_with_issues(payload, manifest)
        defaults = manifest["homework"].get("document_defaults", {})
        active_item_ids = {
            normalize_document_spec(item, role="homework", student=str(item["student"]), defaults=defaults)["item_id"]
            for item in resolved_students
        }
        for homework_item_id, homework_entry in state.get("documents", {}).items():
            if homework_entry.get("role") == "homework" and homework_item_id not in active_item_ids:
                homework_entry.update({"status": "removed", "error": "已从作业索引移除"})
        state["resolved_students"] = resolved_students
        state["index_issues"] = index_issues
    if learned_layout is not None:
        state.setdefault("layouts", {})["homework"] = learned_layout
        state.setdefault("scheduler", {}).pop("layout_probe_item_id", None)
    save_state(manifest, state)
    return {
        "item_id": item_id,
        "status": "success",
        "cache_path": str(cache_path),
        "resolved_students": len(state.get("resolved_students", [])),
        "learned_layout": learned_layout,
    }


def ingest_evidence(manifest: dict[str, Any], item_id: str, evidence_path: Path) -> dict[str, Any]:
    with state_lock(manifest):
        return _ingest_evidence_unlocked(manifest, item_id, evidence_path)


def ingest_evidence_batch(manifest: dict[str, Any], evidence_dir: Path) -> dict[str, Any]:
    if not evidence_dir.is_dir():
        raise ManifestError(f"批量证据目录不存在：{evidence_dir}")
    state = load_state(manifest)
    candidates = {
        item_id: evidence_dir / f"{item_id}.json"
        for item_id, entry in state.get("documents", {}).items()
        if entry.get("status") in {"pending", "in_progress", "needs_discovery"}
    }
    results: list[dict[str, Any]] = []
    missing: list[str] = []
    for item_id, path in candidates.items():
        if not path.exists():
            missing.append(item_id)
            continue
        try:
            results.append(ingest_evidence(manifest, item_id, path))
        except (OSError, json.JSONDecodeError, ManifestError) as exc:
            results.append({"item_id": item_id, "status": "failed", "error": str(exc)})
    return {
        "schema_version": 1,
        "ingested": sum(1 for item in results if item.get("status") == "success"),
        "needs_discovery": sum(1 for item in results if item.get("status") == "needs_discovery"),
        "failed": sum(1 for item in results if item.get("status") == "failed"),
        "missing_files": missing,
        "results": results,
    }


def apply_failure_to_state(
    manifest: dict[str, Any],
    state: dict[str, Any],
    item_id: str,
    error: str,
    error_kind: str | None = None,
) -> dict[str, Any]:
    entry = state.get("documents", {}).get(item_id)
    if entry is None:
        raise ManifestError(f"未知读取项：{item_id}；请先执行plan")
    kind = classify_error_kind(error, error_kind)
    attempts = int(entry.get("attempts", 0)) + 1
    if kind == "layout_mismatch":
        entry.update({
            "status": "needs_discovery",
            "sheet": "",
            "range": "",
            "cache_path": "",
            "read_mode": "discovery_fallback",
            "error": error,
            "error_kind": kind,
            "attempts": attempts,
            "next_attempt_at": "",
            "finished_at": utc_text(),
        })
        return {"item_id": item_id, "status": "needs_discovery", "error_kind": kind, "attempts": attempts, "next_attempt_at": ""}
    retryable = kind in RETRYABLE_ERROR_KINDS and attempts < int(manifest["runtime"]["retries"])
    next_attempt_at = ""
    if retryable:
        delays = manifest["runtime"]["retry_delays_seconds"]
        delay = float(delays[min(attempts - 1, len(delays) - 1)])
        next_attempt_at = utc_text(utc_now() + timedelta(seconds=delay))
    entry.update({
        "status": "pending_retry" if retryable else "failed",
        "error": error,
        "error_kind": kind,
        "attempts": attempts,
        "next_attempt_at": next_attempt_at,
        "finished_at": utc_text(),
    })
    return {"item_id": item_id, "status": entry["status"], "error_kind": kind, "attempts": attempts, "next_attempt_at": next_attempt_at}


def _record_failure_unlocked(manifest: dict[str, Any], item_id: str, error: str, error_kind: str | None = None) -> dict[str, Any]:
    state = load_state(manifest)
    result = apply_failure_to_state(manifest, state, item_id, error, error_kind)
    save_state(manifest, state)
    return result


def record_failure(manifest: dict[str, Any], item_id: str, error: str, error_kind: str | None = None) -> dict[str, Any]:
    with state_lock(manifest):
        return _record_failure_unlocked(manifest, item_id, error, error_kind)


def scoring_documents(manifest: dict[str, Any]) -> tuple[Path | None, list[tuple[str, Path]], list[dict[str, Any]]]:
    state = load_state(manifest)
    standard_path: Path | None = None
    homework_paths: list[tuple[str, Path]] = []
    failures: list[dict[str, Any]] = []
    for item_id, entry in state.get("documents", {}).items():
        role = entry.get("role")
        status = entry.get("status")
        cached = Path(entry.get("cache_path", "")) if entry.get("cache_path") else None
        if role == "standard" and status in {"success", "cached"} and cached and cached.exists():
            standard_path = cached
        elif role == "homework":
            if cached and cached.exists() and status in {"success", "cached"}:
                homework_paths.append((str(entry.get("student") or ""), cached))
            elif status in INCOMPLETE_DOCUMENT_STATUSES:
                failures.append({"student": entry.get("student", ""), "item_id": item_id, "error": entry.get("error") or "等待读取"})
    homework_paths.sort(key=lambda item: item[0])
    return standard_path, homework_paths, failures


def public_workflow_spec(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        key: spec.get(key, "")
        for key in ("item_id", "role", "student", "url", "document_id", "revision", "sheet", "range", "read_mode", "discovery_range")
        if spec.get(key) not in {None, ""}
    }


def workflow_specs_hash(specs: list[dict[str, Any]]) -> str:
    return stable_hash([
        {key: spec.get(key, "") for key in ("item_id", "role", "student", "url", "document_id")}
        for spec in specs
    ])


def workflow_blocked_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    blocked: list[dict[str, Any]] = []
    for item_id, entry in state.get("documents", {}).items():
        if entry.get("status") != "failed":
            continue
        blocked.append({
            "stage": "evidence",
            "item_id": item_id,
            "role": entry.get("role", ""),
            "student": entry.get("student", ""),
            "reason": entry.get("error") or "证据未就绪",
            "next_action": "修复权限、链接、身份或证据后复用同一 task_id 续跑",
        })
    for issue in state.get("index_issues", []):
        if not isinstance(issue, dict):
            continue
        blocked.append({
            "stage": issue.get("stage", "homework_index"),
            "item_id": "",
            "role": "homework_index",
            "student": issue.get("student", ""),
            "row_number": issue.get("row_number"),
            "reason": issue.get("error") or "作业索引异常",
            "next_action": issue.get("next_action") or "修订作业索引后复用同一 task_id 续跑",
        })
    for signature, item in state.get("workflow", {}).get("contract_errors", {}).items():
        if int(item.get("attempts", 0)) < INTERNAL_ERROR_LIMIT:
            continue
        blocked.append({
            "stage": "orchestration",
            "item_id": "",
            "role": "workflow",
            "student": "",
            "reason": item.get("reason") or f"内部契约错误已重复{INTERNAL_ERROR_LIMIT}次",
            "next_action": item.get("next_action") or "使用 capabilities --json 核对接口后从原检查点续跑",
            "signature": signature,
        })
    return blocked


def workflow_user_actions(blocked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in blocked:
        stage = str(item.get("stage") or "evidence")
        grouped.setdefault(stage, []).append(item)
    return [
        {
            "type": stage,
            "prompt": "请处理以下项后复用同一 task_id 续跑",
            "items": items,
        }
        for stage, items in grouped.items()
    ]


def workflow_response_payload(
    manifest: dict[str, Any],
    state: dict[str, Any],
    status: str,
    *,
    operation: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    recoveries: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blocked = workflow_blocked_items(state)
    payload: dict[str, Any] = {
        "schema_version": WORKFLOW_VERSION,
        "task_id": manifest["task_id"],
        "workflow_status": status,
        "recoverable": status in {"action_required", "retrying", "ready_to_score"},
        "operation_id": str((operation or {}).get("operation_id") or ""),
        "action": operation or None,
        "warnings": list(warnings or []),
        "recoveries": list(recoveries or []),
        "blocked_items": blocked,
        "user_actions": workflow_user_actions(blocked),
    }
    if extra:
        payload.update(extra)
    return payload


def create_workflow_operation(
    manifest: dict[str, Any],
    state: dict[str, Any],
    *,
    action_type: str,
    stage: str,
    specs: list[dict[str, Any]],
) -> dict[str, Any]:
    workflow = state["workflow"]
    workflow["revision"] = int(workflow.get("revision", 0)) + 1
    specs_digest = workflow_specs_hash(specs)
    operation_id = stable_hash({
        "task_id": manifest["task_id"],
        "action": action_type,
        "stage": stage,
        "specs_hash": specs_digest,
        "revision": workflow["revision"],
    })[:24]
    operation: dict[str, Any] = {
        "type": action_type,
        "stage": stage,
        "task_id": manifest["task_id"],
        "operation_id": operation_id,
        "specs_hash": specs_digest,
        "items": [public_workflow_spec(spec) for spec in specs],
        "created_at": utc_text(),
    }
    if action_type == "preflight_docs":
        operation["response_template"] = {
            "schema_version": WORKFLOW_VERSION,
            "task_id": manifest["task_id"],
            "stage": stage,
            "operation_id": operation_id,
            "specs_hash": specs_digest,
            "items": [
                {
                    "item_id": spec["item_id"],
                    "revision": "",
                    "sheet": "",
                    "range": "",
                }
                for spec in specs
            ],
        }
    elif action_type == "read_docs":
        operation["evidence_contract"] = {
            "directory_file_name": "<item_id>.json",
            "failure_response": {
                "schema_version": WORKFLOW_VERSION,
                "task_id": manifest["task_id"],
                "stage": stage,
                "operation_id": operation_id,
                "items": [{"item_id": "ITEM_ID", "error_kind": "timeout", "error": "Docs timeout"}],
            },
        }
    elif action_type == "score":
        operation["score_args"] = {
            "manifest": manifest["_path"],
            "output": manifest.get("output", {}).get("dir", ""),
        }
    workflow["current_operation"] = operation
    save_state(manifest, state)
    return operation


def normalize_workflow_items(
    payload: Any,
    specs: list[dict[str, Any]],
    operation: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
    warnings: list[str] = []
    recoveries: list[str] = []
    contract_errors: list[str] = []
    expected_by_id = {str(spec["item_id"]): spec for spec in specs}
    by_document: dict[str, list[dict[str, Any]]] = {}
    for spec in specs:
        by_document.setdefault(str(spec.get("document_id") or ""), []).append(spec)

    raw_items: Any
    envelope = payload if isinstance(payload, dict) and "items" in payload else None
    if envelope is not None:
        if envelope.get("task_id") not in {None, "", operation.get("task_id"), operation.get("manifest_task_id")} and str(envelope.get("task_id")) != str(operation.get("task_id") or ""):
            contract_errors.append("workflow快照task_id与当前任务不一致")
        if envelope.get("operation_id") and str(envelope["operation_id"]) != str(operation["operation_id"]):
            contract_errors.append("workflow快照operation_id已过期")
        if envelope.get("specs_hash") and str(envelope["specs_hash"]) != str(operation["specs_hash"]):
            contract_errors.append("workflow快照specs_hash已过期")
        raw_items = envelope.get("items")
    elif isinstance(payload, list):
        raw_items = payload
        recoveries.append("已将顶层数组自动规范化为workflow v2 items")
    elif isinstance(payload, dict):
        raw_items = []
        for raw_key, raw_value in payload.items():
            if raw_key in {"schema_version", "task_id", "stage", "operation_id", "specs_hash"}:
                continue
            item = dict(raw_value) if isinstance(raw_value, dict) else {"revision": raw_value}
            item.setdefault("lookup_key", str(raw_key))
            raw_items.append(item)
        recoveries.append("已将映射型revision快照自动规范化为workflow v2 items")
    else:
        return [], warnings, recoveries, ["预检响应必须是数组或JSON对象"]
    if not isinstance(raw_items, list):
        return [], warnings, recoveries, ["预检响应items必须是数组"]

    normalized_by_id: dict[str, dict[str, Any]] = {}
    conflicted: set[str] = set()
    for position, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            contract_errors.append(f"预检第{position}项不是对象")
            continue
        item_id = str(raw.get("item_id") or "")
        lookup_key = str(raw.get("lookup_key") or raw.get("document_id") or "")
        if not item_id and lookup_key:
            if lookup_key in expected_by_id:
                item_id = lookup_key
                recoveries.append(f"已将映射键 {lookup_key} 识别为item_id")
            elif len(by_document.get(lookup_key, [])) == 1:
                item_id = str(by_document[lookup_key][0]["item_id"])
                recoveries.append(f"已将文档ID {lookup_key} 自动转换为item_id={item_id}")
            elif len(by_document.get(lookup_key, [])) > 1:
                contract_errors.append(f"文档ID {lookup_key} 对应多个读取项，必须回传item_id")
                continue
        if item_id not in expected_by_id:
            warnings.append(f"已忽略未知预检项：{item_id or lookup_key or position}")
            continue
        revision = raw.get("revision", "")
        if isinstance(revision, bool) or isinstance(revision, (dict, list)):
            contract_errors.append(f"item_id={item_id} 的revision结构无效")
            continue
        item = {
            "item_id": item_id,
            "revision": "" if revision is None else str(revision),
            "sheet": "" if raw.get("sheet") is None else str(raw.get("sheet") or ""),
            "range": "" if raw.get("range") is None else str(raw.get("range") or ""),
            "error": str(raw.get("error") or ""),
            "error_kind": str(raw.get("error_kind") or ""),
        }
        if not item["revision"] and not item["error"]:
            warnings.append(f"item_id={item_id} 没有revision，将每次重读并使用内容哈希")
        previous = normalized_by_id.get(item_id)
        if previous is not None and stable_hash(previous) != stable_hash(item):
            conflicted.add(item_id)
            normalized_by_id.pop(item_id, None)
            contract_errors.append(f"item_id={item_id} 出现冲突的重复预检结果")
            continue
        if previous is not None:
            recoveries.append(f"已去重一致的预检项：item_id={item_id}")
        elif item_id not in conflicted:
            normalized_by_id[item_id] = item
    missing = sorted(set(expected_by_id) - set(normalized_by_id) - conflicted)
    if missing:
        warnings.append("以下项未回传，将在下一轮单独预检：" + ",".join(missing))
    return list(normalized_by_id.values()), warnings, recoveries, contract_errors


def record_workflow_contract_errors(state: dict[str, Any], errors: list[str]) -> bool:
    blocked = False
    registry = state["workflow"].setdefault("contract_errors", {})
    for reason in errors:
        signature = stable_hash(reason)[:20]
        item = registry.setdefault(signature, {"attempts": 0, "reason": reason})
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["last_seen_at"] = utc_text()
        item["next_action"] = "使用 capabilities --json 核对接口，然后使用当前workflow response_template重试"
        blocked = blocked or item["attempts"] >= INTERNAL_ERROR_LIMIT
    return blocked


def apply_preflight_operation(manifest: dict[str, Any], response_path: Path) -> dict[str, Any]:
    payload = json.loads(response_path.read_text(encoding="utf-8"))
    with state_lock(manifest):
        state = load_state(manifest)
        operation = state["workflow"].get("current_operation") or {}
        if operation.get("type") != "preflight_docs":
            return workflow_response_payload(manifest, state, "action_required", operation=operation, warnings=["当前操作不接受预检响应"])
        operation_id = str(operation["operation_id"])
        if operation_id in state["workflow"].get("applied_operations", {}):
            state["workflow"]["current_operation"] = {}
            save_state(manifest, state)
            return {"duplicate": True, "warnings": ["重复的operation_id已忽略"], "recoveries": []}
        all_specs = document_specs(manifest, state, stage=str(operation["stage"]))
        requested_ids = {str(item.get("item_id") or "") for item in operation.get("items", []) if isinstance(item, dict)}
        specs = [spec for spec in all_specs if str(spec["item_id"]) in requested_ids]
        normalized, warnings, recoveries, contract_errors = normalize_workflow_items(payload, specs, operation)
        engineering_blocked = False
        if contract_errors:
            engineering_blocked = record_workflow_contract_errors(state, contract_errors)
        fatal_contract_error = any("过期" in reason or "task_id" in reason for reason in contract_errors)
        if contract_errors and (not normalized or fatal_contract_error):
            save_state(manifest, state)
            return {
                "contract_error": True,
                "engineering_blocked": engineering_blocked,
                "warnings": warnings,
                "recoveries": recoveries,
                "contract_errors": contract_errors,
            }
        warnings.extend(contract_errors)
        by_id = {item["item_id"]: item for item in normalized}
        stage_state = state["workflow"].setdefault("preflight", {}).setdefault(str(operation["stage"]), {})
        for spec in specs:
            item_id = str(spec["item_id"])
            update = by_id.get(item_id)
            if update is None:
                stage_state[item_id] = {"status": "pending"}
                continue
            entry = state["documents"].setdefault(item_id, {})
            entry.update({key: spec.get(key, "") for key in ("role", "student", "url", "document_id")})
            entry.update({key: update.get(key, "") for key in ("revision", "sheet", "range")})
            if update.get("error") or update.get("error_kind"):
                error = str(update.get("error") or "预检失败")
                kind = classify_error_kind(error, str(update.get("error_kind") or "") or None)
                attempts = int(entry.get("attempts", 0)) + 1
                retryable = kind in RETRYABLE_ERROR_KINDS and attempts < int(manifest["runtime"]["retries"])
                entry.update({
                    "status": "preflight_retry" if retryable else "failed",
                    "preflight_status": "retry" if retryable else "error",
                    "error": error,
                    "error_kind": kind,
                    "attempts": attempts,
                    "next_attempt_at": utc_text(utc_now() + timedelta(seconds=float(manifest["runtime"]["retry_delays_seconds"][min(attempts - 1, len(manifest["runtime"]["retry_delays_seconds"]) - 1)]))) if retryable else "",
                })
                stage_state[item_id] = {"status": entry["preflight_status"], "checked_at": utc_text()}
            else:
                entry.update({"status": "preflight_ok", "preflight_status": "ok", "error": "", "error_kind": "", "next_attempt_at": ""})
                stage_state[item_id] = {"status": "ok", "checked_at": utc_text(), "revision": update.get("revision", "")}
        state["workflow"].setdefault("applied_operations", {})[operation_id] = {"applied_at": utc_text(), "type": "preflight_docs"}
        state["workflow"]["current_operation"] = {}
        state["workflow"].setdefault("recoveries", []).extend(recoveries)
        save_state(manifest, state)
        return {"warnings": warnings, "recoveries": recoveries, "contract_errors": [], "engineering_blocked": engineering_blocked}


def parse_failure_response(path: Path | None) -> tuple[list[dict[str, str]], dict[str, str]]:
    if path is None:
        return [], {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ManifestError("读取失败响应必须是数组或含items数组的对象")
    failures: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict) or not str(item.get("item_id") or ""):
            raise ManifestError("读取失败项必须包含item_id")
        if not item.get("error") and not item.get("error_kind"):
            continue
        failures.append({
            "item_id": str(item["item_id"]),
            "error": str(item.get("error") or "Docs读取失败"),
            "error_kind": str(item.get("error_kind") or ""),
        })
    envelope = {
        key: str(payload.get(key) or "")
        for key in ("task_id", "operation_id", "specs_hash")
    } if isinstance(payload, dict) else {}
    return failures, envelope


def apply_read_operation(manifest: dict[str, Any], evidence_dir: Path, response_path: Path | None = None) -> dict[str, Any]:
    state = load_state(manifest)
    operation = state.get("workflow", {}).get("current_operation") or {}
    if operation.get("type") != "read_docs":
        return {"warnings": ["当前操作不接受Docs证据"], "recoveries": []}
    operation_id = str(operation["operation_id"])
    if operation_id in state["workflow"].get("applied_operations", {}):
        return {"duplicate": True, "warnings": ["重复的operation_id已忽略"], "recoveries": []}
    failures, envelope = parse_failure_response(response_path)
    contract_errors: list[str] = []
    if envelope.get("task_id") and envelope["task_id"] != str(manifest["task_id"]):
        contract_errors.append("读取失败响应task_id与当前任务不一致")
    if envelope.get("operation_id") and envelope["operation_id"] != operation_id:
        contract_errors.append("读取失败响应operation_id已过期")
    if envelope.get("specs_hash") and envelope["specs_hash"] != str(operation.get("specs_hash") or ""):
        contract_errors.append("读取失败响应specs_hash已过期")
    if contract_errors:
        with state_lock(manifest):
            state = load_state(manifest)
            engineering_blocked = record_workflow_contract_errors(state, contract_errors)
            save_state(manifest, state)
        return {
            "contract_error": True,
            "engineering_blocked": engineering_blocked,
            "contract_errors": contract_errors,
            "warnings": [],
            "recoveries": [],
        }
    batch = ingest_evidence_batch(manifest, evidence_dir)
    expected_ids = {
        str(item.get("item_id") or "")
        for item in operation.get("items", [])
        if isinstance(item, dict)
    }
    ignored_failures: list[str] = []
    with state_lock(manifest):
        state = load_state(manifest)
        if operation_id in state["workflow"].get("applied_operations", {}):
            return {"duplicate": True, "warnings": ["重复的operation_id已忽略"], "recoveries": []}
        current = state["workflow"].get("current_operation") or {}
        if str(current.get("operation_id") or "") != operation_id:
            return {"warnings": ["读取响应已过期，已保留当前检查点"], "recoveries": []}
        failure_results: list[dict[str, Any]] = []
        for failure in failures:
            if failure["item_id"] not in expected_ids:
                ignored_failures.append(failure["item_id"])
                continue
            failure_results.append(apply_failure_to_state(
                manifest,
                state,
                failure["item_id"],
                failure["error"],
                failure["error_kind"] or None,
            ))
        state["workflow"].setdefault("applied_operations", {})[operation_id] = {"applied_at": utc_text(), "type": "read_docs"}
        state["workflow"]["current_operation"] = {}
        save_state(manifest, state)
    warnings = [f"{len(batch.get('missing_files', []))}份证据文件未回传"] if batch.get("missing_files") else []
    if ignored_failures:
        warnings.append("已忽略不属于当前操作的失败项：" + ",".join(sorted(ignored_failures)))
    return {
        "warnings": warnings,
        "recoveries": [f"已入库{batch.get('ingested', 0)}份，布局回退{batch.get('needs_discovery', 0)}份，读取失败{len(failure_results)}份"],
        "batch": batch,
    }


def finalize_workflow_result(manifest: dict[str, Any], result_path: Path, operation_id: str) -> dict[str, Any]:
    result = json.loads(result_path.read_text(encoding="utf-8"))
    with state_lock(manifest):
        state = load_state(manifest)
        operation = state["workflow"].get("current_operation") or {}
        if operation.get("type") != "score" or str(operation.get("operation_id") or "") != str(operation_id or ""):
            return workflow_response_payload(manifest, state, "action_required", operation=operation, warnings=["评分结果的operation_id已过期或不匹配"])
        run_status = str(result.get("run_status") or "")
        terminal_status = "complete" if run_status == "complete" else "awaiting_user"
        terminal = {
            "workflow_status": terminal_status,
            "result_json": str(result_path.resolve()),
            "run_status": run_status,
            "stopped_items": list(result.get("stopped_items") or []),
            "user_actions": list(result.get("user_actions") or []),
            "finished_at": utc_text(),
        }
        state["workflow"].setdefault("applied_operations", {})[operation_id] = {"applied_at": utc_text(), "type": "score"}
        state["workflow"]["current_operation"] = {}
        state["workflow"]["terminal"] = terminal
        save_state(manifest, state)
        payload = workflow_response_payload(manifest, state, terminal_status, extra=terminal)
        if terminal.get("user_actions"):
            payload["user_actions"] = terminal["user_actions"]
        return payload


def reset_workflow(manifest: dict[str, Any]) -> None:
    with state_lock(manifest):
        state = load_state(manifest)
        state["workflow"].update({"current_operation": {}, "preflight": {}, "terminal": {}, "contract_errors": {}})
        for entry in state.get("documents", {}).values():
            entry.pop("preflight_status", None)
            entry.pop("metadata_refresh_attempts", None)
            if entry.get("status") != "removed":
                entry.update({
                    "status": "pending",
                    "error": "",
                    "error_kind": "",
                    "attempts": 0,
                    "next_attempt_at": "",
                })
        save_state(manifest, state)


def workflow_next(manifest: dict[str, Any]) -> dict[str, Any]:
    state = load_state(manifest)
    terminal = state.get("workflow", {}).get("terminal") or {}
    if terminal:
        payload = workflow_response_payload(manifest, state, str(terminal.get("workflow_status") or "awaiting_user"), extra=terminal)
        if terminal.get("user_actions"):
            payload["user_actions"] = terminal["user_actions"]
        return payload
    current = state.get("workflow", {}).get("current_operation") or {}
    if current:
        status = "ready_to_score" if current.get("type") == "score" else "action_required"
        return workflow_response_payload(manifest, state, status, operation=current)

    initial_specs = document_specs(manifest, state, stage="initial")
    initial_done = all(
        state.get("documents", {}).get(spec["item_id"], {}).get("status") in {"success", "cached", "failed"}
        for spec in initial_specs
    )
    stage = "students" if initial_done else "initial"
    specs = document_specs(manifest, state, stage=stage)
    if stage == "students" and not specs:
        operation = create_workflow_operation(manifest, state, action_type="score", stage="score", specs=[])
        return workflow_response_payload(manifest, load_state(manifest), "ready_to_score", operation=operation)

    pending_preflight: list[dict[str, Any]] = []
    waiting_preflight_retry: list[dict[str, Any]] = []
    prepared: list[dict[str, Any]] = []
    for spec in specs:
        entry = state.get("documents", {}).get(spec["item_id"], {})
        if entry.get("status") in {"success", "cached", "failed"}:
            clone = dict(spec)
            clone["preflight_ok"] = entry.get("status") in {"success", "cached"}
            if entry.get("status") == "failed":
                clone["workflow_terminal_failure"] = True
            prepared.append(clone)
            continue
        if entry.get("preflight_status") not in {"ok", "error", "retry"}:
            pending_preflight.append(spec)
            clone = dict(spec)
            clone["workflow_preflight_pending"] = True
            prepared.append(clone)
            continue
        if entry.get("preflight_status") == "retry":
            try:
                next_attempt = datetime.fromisoformat(str(entry.get("next_attempt_at") or ""))
            except ValueError:
                next_attempt = utc_now()
            clone = dict(spec)
            clone["workflow_preflight_pending"] = True
            prepared.append(clone)
            if next_attempt > utc_now():
                waiting_preflight_retry.append({**spec, "error": entry.get("error", ""), "error_kind": entry.get("error_kind", ""), "next_attempt_at": entry.get("next_attempt_at", "")})
            else:
                pending_preflight.append(spec)
            continue
        clone = dict(spec)
        if entry.get("preflight_status") == "ok":
            clone.update({key: entry.get(key, clone.get(key, "")) for key in ("revision", "sheet", "range")})
            clone["preflight_ok"] = True
        elif entry.get("preflight_status") in {"error", "retry"}:
            clone["preflight_error"] = entry.get("error", "")
            clone["preflight_error_kind"] = entry.get("error_kind", "")
        prepared.append(clone)

    plan = plan_reads(manifest, prepared)
    state = load_state(manifest)
    if plan["read"]:
        operation = create_workflow_operation(manifest, state, action_type="read_docs", stage=stage, specs=plan["read"])
        return workflow_response_payload(manifest, load_state(manifest), "action_required", operation=operation)
    if pending_preflight:
        operation = create_workflow_operation(manifest, state, action_type="preflight_docs", stage=stage, specs=pending_preflight)
        return workflow_response_payload(manifest, load_state(manifest), "action_required", operation=operation)
    if waiting_preflight_retry:
        next_attempts = sorted(str(item.get("next_attempt_at") or "") for item in waiting_preflight_retry if item.get("next_attempt_at"))
        return workflow_response_payload(
            manifest,
            state,
            "retrying",
            extra={"retry_items": waiting_preflight_retry, "next_attempt_at": next_attempts[0] if next_attempts else ""},
        )
    if plan["retry"]:
        next_attempts = sorted(str(item.get("next_attempt_at") or "") for item in plan["retry"] if item.get("next_attempt_at"))
        return workflow_response_payload(
            manifest,
            state,
            "retrying",
            extra={"retry_items": plan["retry"], "next_attempt_at": next_attempts[0] if next_attempts else ""},
        )
    if stage == "initial":
        return workflow_next(manifest)
    operation = create_workflow_operation(manifest, state, action_type="score", stage="score", specs=[])
    status = "engineering_blocked" if any(item.get("stage") == "orchestration" for item in workflow_blocked_items(load_state(manifest))) else "ready_to_score"
    return workflow_response_payload(manifest, load_state(manifest), status, operation=operation)


def workflow_command(
    manifest: dict[str, Any],
    *,
    response: Path | None = None,
    evidence_dir: Path | None = None,
    result_json: Path | None = None,
    operation_id: str = "",
    refresh: bool = False,
) -> dict[str, Any]:
    if refresh:
        reset_workflow(manifest)
    warnings: list[str] = []
    recoveries: list[str] = []
    if result_json is not None:
        return finalize_workflow_result(manifest, result_json, operation_id)
    if evidence_dir is not None:
        applied = apply_read_operation(manifest, evidence_dir, response)
        warnings.extend(applied.get("warnings", []))
        recoveries.extend(applied.get("recoveries", []))
        if applied.get("contract_error"):
            state = load_state(manifest)
            status = "engineering_blocked" if applied.get("engineering_blocked") else "action_required"
            return workflow_response_payload(
                manifest,
                state,
                status,
                operation=state["workflow"].get("current_operation") or None,
                warnings=warnings + list(applied.get("contract_errors") or []),
                recoveries=recoveries,
            )
    elif response is not None:
        try:
            response_payload = json.loads(response.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            response_payload = None
        response_operation_id = str(response_payload.get("operation_id") or "") if isinstance(response_payload, dict) else ""
        if response_operation_id and response_operation_id in load_state(manifest).get("workflow", {}).get("applied_operations", {}):
            next_payload = workflow_next(manifest)
            next_payload["recoveries"] = [f"已忽略重复提交的operation_id={response_operation_id}"] + list(next_payload.get("recoveries") or [])
            return next_payload
        applied = apply_preflight_operation(manifest, response)
        warnings.extend(applied.get("warnings", []))
        recoveries.extend(applied.get("recoveries", []))
        if applied.get("contract_error"):
            state = load_state(manifest)
            status = "engineering_blocked" if applied.get("engineering_blocked") else "action_required"
            return workflow_response_payload(
                manifest,
                state,
                status,
                operation=state["workflow"].get("current_operation") or None,
                warnings=warnings + list(applied.get("contract_errors") or []),
                recoveries=recoveries,
            )
    next_payload = workflow_next(manifest)
    next_payload["warnings"] = warnings + list(next_payload.get("warnings") or [])
    next_payload["recoveries"] = recoveries + list(next_payload.get("recoveries") or [])
    return next_payload


def cli() -> int:
    parser = ManifestArgumentParser(description="Manifest证据缓存与续跑状态管理")
    sub = parser.add_subparsers(dest="command", required=True)
    capabilities = sub.add_parser("capabilities")
    capabilities.add_argument("--json", action="store_true", help="输出机器可读能力契约")
    workflow = sub.add_parser("workflow")
    workflow.add_argument("--manifest", required=True, type=Path)
    workflow.add_argument("--response", type=Path, help="当前预检或读取失败响应JSON")
    workflow.add_argument("--evidence-dir", type=Path, help="当前read_docs操作的证据目录")
    workflow.add_argument("--result-json", type=Path, help="当前score操作生成的评分JSON")
    workflow.add_argument("--operation-id", default="", help="提交评分结果时回传的操作ID")
    workflow.add_argument("--refresh", action="store_true", help="开始新一轮预检，保留已有证据缓存")
    specs = sub.add_parser("specs")
    specs.add_argument("--manifest", required=True, type=Path)
    specs.add_argument("--stage", choices=["initial", "students", "all"], default="all")
    plan = sub.add_parser("plan")
    plan.add_argument("--manifest", required=True, type=Path)
    plan.add_argument("--stage", choices=["initial", "students", "all"], default="all")
    plan.add_argument("--revisions", type=Path)
    plan.add_argument("--refresh", action="store_true")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--manifest", required=True, type=Path)
    ingest.add_argument("--item-id", required=True)
    ingest.add_argument("--evidence", required=True, type=Path)
    ingest_batch = sub.add_parser("ingest-batch")
    ingest_batch.add_argument("--manifest", required=True, type=Path)
    ingest_batch.add_argument("--evidence-dir", required=True, type=Path)
    fail = sub.add_parser("fail")
    fail.add_argument("--manifest", required=True, type=Path)
    fail.add_argument("--item-id", required=True)
    fail.add_argument("--error", required=True)
    fail.add_argument("--error-kind", choices=sorted(RETRYABLE_ERROR_KINDS | FATAL_ERROR_KINDS | SPECIAL_ERROR_KINDS))
    status = sub.add_parser("status")
    status.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "capabilities":
        print(json.dumps(capabilities_payload(), ensure_ascii=False, indent=2))
        return 0
    manifest = load_manifest(args.manifest)
    if args.command == "workflow":
        selected_inputs = sum(value is not None for value in (args.evidence_dir, args.result_json))
        if selected_inputs > 1:
            raise ManifestCliError("workflow每次只能提交证据目录或评分JSON中的一种")
        print(json.dumps(workflow_command(
            manifest,
            response=args.response,
            evidence_dir=args.evidence_dir,
            result_json=args.result_json,
            operation_id=args.operation_id,
            refresh=args.refresh,
        ), ensure_ascii=False, indent=2))
    elif args.command == "specs":
        state = load_state(manifest)
        print(json.dumps({"schema_version": 1, "task_id": manifest["task_id"], "items": document_specs(manifest, state, stage=args.stage)}, ensure_ascii=False, indent=2))
    elif args.command == "plan":
        state = load_state(manifest)
        specs = merge_revisions(document_specs(manifest, state, stage=args.stage), args.revisions)
        print(json.dumps(plan_reads(manifest, specs, refresh=args.refresh), ensure_ascii=False, indent=2))
    elif args.command == "ingest":
        print(json.dumps(ingest_evidence(manifest, args.item_id, args.evidence), ensure_ascii=False))
    elif args.command == "ingest-batch":
        print(json.dumps(ingest_evidence_batch(manifest, args.evidence_dir), ensure_ascii=False, indent=2))
    elif args.command == "fail":
        print(json.dumps(record_failure(manifest, args.item_id, args.error, args.error_kind), ensure_ascii=False))
    else:
        print(json.dumps(load_state(manifest), ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    try:
        return cli()
    except Exception as exc:
        recoverable = isinstance(exc, ManifestCliError)
        invalid_command = exc.invalid_command if isinstance(exc, ManifestCliError) else ""
        suggestion = ""
        if invalid_command:
            matches = difflib.get_close_matches(invalid_command, WORKFLOW_COMMANDS, n=1, cutoff=0.35)
            suggestion = matches[0] if matches else "workflow"
        print(f"错误：{exc}", file=sys.stderr)
        print(json.dumps({
            "status": "stopped",
            "error_code": "cli_contract_error" if recoverable else "manifest_error",
            "recoverable": recoverable,
            "valid_commands": list(WORKFLOW_COMMANDS) if recoverable else [],
            "suggested_command": suggestion,
            "suggested_retry_command": "python3 scripts/manifest_runtime.py capabilities --json" if invalid_command else "python3 scripts/manifest_runtime.py workflow --manifest /absolute/path/to/task.json",
            "stopped_items": [{
                "stage": "manifest",
                "reason": str(exc),
                "next_action": "执行 capabilities --json 并使用workflow统一入口自动重试" if recoverable else "修复Manifest、证据、权限或读取状态后，复用同一 task_id 继续执行",
            }],
        }, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
