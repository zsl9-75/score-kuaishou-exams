#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MAX_CONCURRENCY = 15
DEFAULT_RETRIES = 3
RETRYABLE_ERROR_KINDS = {"429", "timeout", "transient_5xx"}
FATAL_ERROR_KINDS = {"permission", "not_found", "invalid_evidence", "metadata_mismatch", "other"}
SPECIAL_ERROR_KINDS = {"layout_mismatch"}
INCOMPLETE_DOCUMENT_STATUSES = {"pending", "failed", "deferred_layout", "pending_retry", "preflight_retry", "needs_discovery", "in_progress"}
PROFILE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "references" / "exam_profiles.json"


class ManifestError(RuntimeError):
    pass


class LayoutMismatch(ManifestError):
    pass


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


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
    document_id = str(merged.get("id") or document.get("id") or document_id_from_url(url))
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
    runtime.setdefault("retry_delays_seconds", [1, 2, 4])
    delays = runtime["retry_delays_seconds"]
    if not isinstance(delays, list) or len(delays) < retries or any(not isinstance(value, (int, float)) or value < 0 for value in delays):
        raise ManifestError("retry_delays_seconds 必须是长度不小retries的非负数数组")
    runtime.setdefault("claim_timeout_seconds", 300)
    runtime.setdefault("learn_homework_layout", True)
    runtime.setdefault("ocr_workers", 4)
    runtime.setdefault("ocr_confidence_threshold", 0.75)
    if not 1 <= int(runtime["ocr_workers"]) <= 8:
        raise ManifestError("ocr_workers 必须在 1–8 之间")
    if not 0 <= float(runtime["ocr_confidence_threshold"]) <= 1:
        raise ManifestError("ocr_confidence_threshold 必须在 0–1 之间")
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
    return cache_root(manifest) / "runs" / f"{safe_task}.json"


def load_state(manifest: dict[str, Any]) -> dict[str, Any]:
    path = state_path(manifest)
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
        }
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"任务状态损坏：{path}：{exc}") from exc
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
    return state


def save_state(manifest: dict[str, Any], state: dict[str, Any]) -> None:
    atomic_write_json(state_path(manifest), state)


def evidence_cache_key(spec: dict[str, Any]) -> str | None:
    if not spec.get("revision"):
        return None
    return stable_hash({key: spec.get(key, "") for key in ("document_id", "revision", "sheet", "range")})


def evidence_cache_path(manifest: dict[str, Any], spec: dict[str, Any]) -> Path | None:
    key = evidence_cache_key(spec)
    return cache_root(manifest) / "evidence" / f"{key}.json" if key else None


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
    by_id = {str(item.get("item_id")): item for item in items if isinstance(item, dict)}
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
    if path and path.exists():
        return path
    entry = state.get("documents", {}).get(spec["item_id"], {})
    cached = Path(entry.get("cache_path", "")) if entry.get("cache_path") else None
    if cached and cached.exists() and all(str(entry.get(key, "")) == str(spec.get(key, "")) for key in ("document_id", "revision", "sheet", "range")):
        return cached
    return None


def plan_reads(manifest: dict[str, Any], specs: list[dict[str, Any]], *, refresh: bool = False) -> dict[str, Any]:
    state = load_state(manifest)
    specs = prepare_homework_layout_specs(manifest, state, specs)
    cached: list[dict[str, Any]] = []
    reads: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    retry: list[dict[str, Any]] = []
    for spec in specs:
        entry = state["documents"].setdefault(spec["item_id"], {})
        entry.update({key: spec.get(key, "") for key in ("role", "student", "url", "document_id", "revision", "sheet", "range", "read_mode")})
        if spec.get("deferred_layout"):
            entry.update({"status": "deferred_layout", "error": "等待本次考核首份作业固化页签和范围"})
            deferred.append(spec)
            continue
        preflight_error = str(spec.get("preflight_error") or "")
        if preflight_error:
            error_kind = classify_error_kind(preflight_error, str(spec.get("preflight_error_kind") or "") or None)
            entry.update({"error": preflight_error, "error_kind": error_kind})
            if error_kind in RETRYABLE_ERROR_KINDS:
                entry["status"] = "preflight_retry"
                retry.append({**spec, "error": preflight_error, "error_kind": error_kind})
            else:
                entry["status"] = "failed"
                failed.append({**spec, "error": preflight_error, "error_kind": error_kind})
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


def load_evidence_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ManifestError(f"证据必须是 schema_version=1 的JSON对象：{path}")
    return payload


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
    if identity_mismatches:
        raise ManifestError("证据metadata与读取计划不一致：" + "；".join(identity_mismatches + layout_mismatches))
    if layout_mismatches:
        message = "证据metadata的页签/范围与快速读取计划不一致：" + "；".join(layout_mismatches)
        if entry.get("read_mode") == "learned_fast":
            raise LayoutMismatch(message)
        raise ManifestError(message)
    return actual


def resolve_index_students(payload: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
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
        raise ManifestError("作业索引缺少姓名列或作业链接列")
    defaults = homework.get("document_defaults", {})
    students: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, list):
            continue
        name = str(row[name_index] if name_index < len(row) else "").strip()
        url = str(row[link_index] if link_index < len(row) else "").strip()
        if not name and not url:
            continue
        if not name or not url:
            raise ManifestError("作业索引存在姓名或链接为空的行")
        if name in seen:
            raise ManifestError(f"作业索引出现重复学员：{name}")
        seen.add(name)
        students.append({"student": name, "url": url, "id": document_id_from_url(url), **defaults})
    return students


def ingest_evidence(manifest: dict[str, Any], item_id: str, evidence_path: Path) -> dict[str, Any]:
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
        entry.update({"status": "failed", "error_kind": "invalid_evidence" if not isinstance(exc, ManifestError) or "metadata" not in str(exc) else "metadata_mismatch", "error": str(exc), "finished_at": utc_text()})
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
        resolved_students = resolve_index_students(payload, manifest)
        defaults = manifest["homework"].get("document_defaults", {})
        active_item_ids = {
            normalize_document_spec(item, role="homework", student=str(item["student"]), defaults=defaults)["item_id"]
            for item in resolved_students
        }
        for homework_item_id, homework_entry in state.get("documents", {}).items():
            if homework_entry.get("role") == "homework" and homework_item_id not in active_item_ids:
                homework_entry.update({"status": "removed", "error": "已从作业索引移除"})
        state["resolved_students"] = resolved_students
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


def record_failure(manifest: dict[str, Any], item_id: str, error: str, error_kind: str | None = None) -> dict[str, Any]:
    state = load_state(manifest)
    entry = state.get("documents", {}).setdefault(item_id, {})
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
        save_state(manifest, state)
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
    save_state(manifest, state)
    return {"item_id": item_id, "status": entry["status"], "error_kind": kind, "attempts": attempts, "next_attempt_at": next_attempt_at}


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


def cli() -> int:
    parser = argparse.ArgumentParser(description="Manifest证据缓存与续跑状态管理")
    sub = parser.add_subparsers(dest="command", required=True)
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
    manifest = load_manifest(args.manifest)
    if args.command == "specs":
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


if __name__ == "__main__":
    raise SystemExit(cli())
