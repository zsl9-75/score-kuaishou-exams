#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ocr_api import ApiOcrError, recognize_many as run_api_ocr

from manifest_runtime import (
    INCOMPLETE_DOCUMENT_STATUSES,
    ManifestError,
    atomic_write_json,
    cache_root,
    canonical_evidence,
    document_specs,
    ingest_evidence,
    load_manifest,
    load_state,
    plan_reads,
    resolve_path,
    scoring_documents,
    stable_hash,
    validate_evidence_payload,
)


SCRIPT_DIR = Path(__file__).resolve().parent
if (SCRIPT_DIR.parent / "references" / "exam_profiles.json").is_file():
    SKILL_ROOT = SCRIPT_DIR.parent
    CONFIG_PATH = SKILL_ROOT / "references" / "exam_profiles.json"
    VISION_SCRIPT = SKILL_ROOT / "scripts" / "ocr_vision.swift"
elif (SCRIPT_DIR / "exam_profiles.json").is_file():
    # Defensive compatibility for platforms that flatten a Skill during import.
    SKILL_ROOT = SCRIPT_DIR
    CONFIG_PATH = SKILL_ROOT / "exam_profiles.json"
    VISION_SCRIPT = SKILL_ROOT / "ocr_vision.swift"
else:
    SKILL_ROOT = SCRIPT_DIR.parent
    CONFIG_PATH = SKILL_ROOT / "references" / "exam_profiles.json"
    VISION_SCRIPT = SKILL_ROOT / "scripts" / "ocr_vision.swift"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}
SCORING_RULE_VERSION = "2026-08-27-evidence-and-ocr-v3"
STANDARD_ANSWER_DELIMITER = re.compile(r"[\r\n,，、/／|｜]+")
STANDARD_COLLECTION_KEYS = ("values", "options", "labels", "items", "selected", "selections", "tags")
STANDARD_SCALAR_KEYS = ("text", "label", "name", "title", "display_value", "displayValue", "value")
COLORS = {
    "header": "#D8E5F7",
    "body": "#F7F8FA",
    "grid": "#FFFFFF",
    "text": "#344054",
    "pending": "#D0D5DD",
    "90": "#6AB37B",
    "80": "#A7D08D",
    "70": "#FEE07B",
    "60": "#F5A05C",
    "low": "#F35161",
}
MAX_PNG_PIXELS = 80_000_000


class AssessmentError(RuntimeError):
    pass


class AssessmentArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AssessmentError(f"命令参数错误：{message}")


def compact_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[\s\u3000]+", "", str(value).strip())


def normalize_header(value: Any) -> str:
    return compact_text(value)


def normalize_name(value: Any, aliases: dict[str, str]) -> str:
    name = compact_text(value)
    return compact_text(aliases.get(name, name))


def normalize_id(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
        return format(value, ".15g")
    text = compact_text(value)
    if not text:
        return ""
    if re.fullmatch(r"[+-]?\d+\.0+", text):
        return str(int(float(text)))
    if re.fullmatch(r"\d+", text):
        return str(int(text))
    return text


def display_cell_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def extract_standard_keyword_tokens(value: Any) -> tuple[list[Any], bool]:
    """Return raw keyword tokens plus whether a structured blank was explicitly present."""
    if value is None:
        return [], True
    if isinstance(value, str):
        if not compact_text(value):
            return [], True
        # Empty fragments caused only by text delimiters are formatting noise.
        return [part for part in STANDARD_ANSWER_DELIMITER.split(value) if compact_text(part)], False
    if isinstance(value, (list, tuple)):
        tokens: list[Any] = []
        has_blank = not value
        for item in value:
            item_tokens, item_has_blank = extract_standard_keyword_tokens(item)
            tokens.extend(item_tokens)
            has_blank = has_blank or item_has_blank
        return tokens, has_blank
    if isinstance(value, dict):
        if not value:
            return [], True
        for key in STANDARD_COLLECTION_KEYS:
            if key in value:
                return extract_standard_keyword_tokens(value[key])

        # Scalar keys are alternative renderings of one tag. Prefer non-blank
        # renderings and do not treat an unused alternative field as a blank option.
        scalar_values = [value[key] for key in STANDARD_SCALAR_KEYS if key in value]
        if scalar_values:
            for scalar in scalar_values:
                scalar_tokens, _ = extract_standard_keyword_tokens(scalar)
                if scalar_tokens:
                    return scalar_tokens, False
            return [], True
        raise AssessmentError(f"标准答案结构无法识别：{display_cell_value(value)}")
    return [value], False


def standard_answer_keywords(value: Any) -> tuple[str, ...] | None:
    tokens, has_structured_blank = extract_standard_keyword_tokens(value)
    if not tokens:
        return None
    if has_structured_blank:
        raise AssessmentError(f"标准答案不能把空值与非空关键词混合：{display_cell_value(value)}")

    keywords: list[str] = []
    for token in tokens:
        keyword = str(token).strip()
        if not compact_text(keyword):
            continue
        if keyword not in keywords:
            keywords.append(keyword)
    return tuple(keywords) or None


def match_standard_keywords(homework_value: Any, keywords: tuple[str, ...]) -> tuple[str, ...]:
    homework_text = compact_text(homework_value)
    if not homework_text:
        return ()
    matches: list[str] = []
    for keyword in keywords:
        keyword_text = compact_text(keyword)
        if keyword_text in homework_text or homework_text in keyword_text:
            matches.append(keyword)
    return tuple(matches)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssessmentError(f"无法读取 JSON：{path}：{exc}") from exc


def load_config() -> dict[str, Any]:
    data = load_json(CONFIG_PATH)
    if data.get("schema_version") != 1 or len(data.get("profiles", {})) != 10:
        raise AssessmentError("考试配置损坏：必须是 schema_version=1 且包含10种考试")
    return data


def load_aliases(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    data = load_json(path)
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise AssessmentError("姓名别名必须是 JSON 对象，例如 {\"简称\": \"完整姓名\"}")
    return {compact_text(k): compact_text(v) for k, v in data.items()}


def normalize_image_confidence(document: dict[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    """Normalize image confidence keys without allowing invalid values to disappear silently."""
    if document.get("source") != "image_ocr":
        return {}, {}
    raw = document.get("confidence")
    if not isinstance(raw, dict):
        return {}, {"__document__": "截图证据缺少逐题 confidence 对象"}
    normalized: dict[str, float] = {}
    issues: dict[str, str] = {}
    for raw_key, raw_value in raw.items():
        row_id = normalize_id(raw_key)
        if not row_id:
            issues["__document__"] = "OCR置信度存在无法规范化的空ID键"
            continue
        if row_id in normalized or row_id in issues:
            issues[row_id] = f"OCR置信度ID规范化后重复：{raw_key}"
            normalized.pop(row_id, None)
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            issues[row_id] = f"OCR置信度必须是0–1有限数值，实际为 {raw_value!r}"
            continue
        value = float(raw_value)
        if not math.isfinite(value) or not 0 <= value <= 1:
            issues[row_id] = f"OCR置信度必须在0–1之间，实际为 {raw_value!r}"
            continue
        normalized[row_id] = value
    return normalized, issues


def confidence_for_row(document: dict[str, Any], row_id: str) -> tuple[float | None, str]:
    if document.get("source") != "image_ocr":
        return None, ""
    confidence = document.get("confidence") if isinstance(document.get("confidence"), dict) else {}
    issues = document.get("confidence_issues") if isinstance(document.get("confidence_issues"), dict) else {}
    if row_id in issues:
        return None, str(issues[row_id])
    if row_id not in confidence:
        general = str(issues.get("__document__") or "")
        return None, general or f"截图证据ID {row_id} 缺少逐题OCR置信度"
    return float(confidence[row_id]), ""


def flatten_documents(payload: Any, source_path: Path) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise AssessmentError(f"证据必须是 JSON 对象：{source_path}")
    documents = payload.get("documents")
    if documents is None:
        documents = [payload]
    if not isinstance(documents, list) or not documents:
        raise AssessmentError(f"证据 documents 必须是非空数组：{source_path}")

    result: list[dict[str, Any]] = []
    for index, doc in enumerate(documents, start=1):
        if not isinstance(doc, dict):
            raise AssessmentError(f"证据第 {index} 项不是对象：{source_path}")
        schema_version = doc.get("schema_version", payload.get("schema_version"))
        if schema_version != 1:
            raise AssessmentError(f"证据 schema_version 必须为 1：{source_path} 第 {index} 项")
        headers = doc.get("headers")
        rows = doc.get("rows")
        if not isinstance(headers, list) or not headers:
            raise AssessmentError(f"证据缺少 headers：{source_path} 第 {index} 项")
        if not isinstance(rows, list):
            raise AssessmentError(f"证据缺少 rows：{source_path} 第 {index} 项")
        normalized_rows: list[list[Any]] = []
        for row_number, row in enumerate(rows, start=2):
            if not isinstance(row, list):
                raise AssessmentError(f"证据第 {row_number} 行必须是数组：{source_path}")
            if len(row) > len(headers):
                raise AssessmentError(f"证据第 {row_number} 行列数超过表头：{source_path}")
            normalized_rows.append(row + [None] * (len(headers) - len(row)))
        clone = dict(doc)
        clone["headers"] = headers
        clone["rows"] = normalized_rows
        confidence, confidence_issues = normalize_image_confidence(clone)
        if clone.get("source") == "image_ocr":
            clone["confidence"] = confidence
            clone["confidence_issues"] = confidence_issues
        clone["_source_path"] = str(source_path)
        try:
            validate_evidence_payload(clone, source_path)
        except ManifestError as exc:
            raise AssessmentError(str(exc)) from exc
        result.append(clone)
    return result


def load_evidence_files(paths: Iterable[Path]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in paths:
        documents.extend(flatten_documents(load_json(path), path))
    return documents


def resolve_header(headers: list[Any], candidates: list[str], label: str, required: bool = True) -> int | None:
    candidate_set = {normalize_header(value) for value in candidates}
    normalized = [normalize_header(value) for value in headers]
    matches = [index for index, value in enumerate(normalized) if value in candidate_set]
    if not matches:
        matches = [
            index
            for index, value in enumerate(normalized)
            if value and any(candidate and (candidate in value or value in candidate) for candidate in candidate_set)
        ]
    if len(matches) > 1:
        raise AssessmentError(f"表头 {label} 出现重复列：{matches}")
    if not matches:
        if required:
            raise AssessmentError(f"缺少表头：{label}")
        return None
    return matches[0]


def dimension_indexes(headers: list[Any], dimensions: list[str], config: dict[str, Any]) -> dict[str, int]:
    indexes: dict[str, int] = {}
    for dimension in dimensions:
        aliases = config["header_aliases"].get(dimension, [dimension])
        indexes[dimension] = int(resolve_header(headers, aliases, dimension, True))
    return indexes


def evidence_index(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for doc in documents:
        public_doc = canonical_evidence({key: value for key, value in doc.items() if not key.startswith("_")})
        digest = sha256_bytes(json.dumps(public_doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        metadata = doc.get("document") if isinstance(doc.get("document"), dict) else {}
        entries.append(
            {
                "source": doc.get("source", "docs"),
                "source_path": doc.get("_source_path", ""),
                "url": metadata.get("url", ""),
                "document_id": metadata.get("id", ""),
                "revision": metadata.get("revision", ""),
                "sheet": doc.get("sheet", ""),
                "range": doc.get("range", ""),
                "read_at": doc.get("read_at", ""),
                "content_sha256": digest,
            }
        )
    return entries


def standard_answer_evidence(
    standard: dict[str, OrderedDict[str, dict[str, Any]]],
    dimensions: list[str],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for dimension in dimensions:
        for row_id, answer in standard[dimension].items():
            keywords = answer["keywords"]
            entries.append(
                {
                    "dimension": dimension,
                    "id": row_id,
                    "row_number": answer["row_number"],
                    "raw_cell": answer["raw_value"],
                    "keywords": list(keywords) if keywords else [],
                    "standard_blank": keywords is None,
                    "source": answer.get("source", ""),
                    "confidence": answer.get("confidence"),
                    "confidence_issue": answer.get("confidence_issue", ""),
                }
            )
    return entries


def parse_standard(documents: list[dict[str, Any]], dimensions: list[str], config: dict[str, Any]) -> dict[str, OrderedDict[str, dict[str, Any]]]:
    standard: dict[str, OrderedDict[str, dict[str, Any]]] = {dim: OrderedDict() for dim in dimensions}
    seen_ids: set[str] = set()

    for doc in documents:
        headers = doc["headers"]
        id_index = int(resolve_header(headers, config["id_headers"], "ID", True))
        dim_indexes = dimension_indexes(headers, dimensions, config)
        for row_number, row in enumerate(doc["rows"], start=2):
            row_id = normalize_id(row[id_index])
            has_dimension_value = any(compact_text(row[index]) for index in dim_indexes.values())
            if not row_id:
                if has_dimension_value:
                    raise AssessmentError(f"标准答案第 {row_number} 行有答案但 ID 为空")
                continue
            if row_id in seen_ids:
                raise AssessmentError(f"标准答案出现重复 ID：{row_id}")
            seen_ids.add(row_id)
            confidence, confidence_issue = confidence_for_row(doc, row_id)
            for dimension, index in dim_indexes.items():
                raw = row[index]
                keywords = standard_answer_keywords(raw)
                blank_allowed = set(config.get("blank_standard_allowed") or [])
                if keywords is None and dimension not in blank_allowed:
                    raise AssessmentError(f"标准答案 ID {row_id} 的“{dimension}”为空；该维度不允许标准空值")
                standard[dimension][row_id] = {
                    "raw": display_cell_value(raw),
                    "raw_value": raw,
                    "keywords": keywords,
                    "row_number": row_number,
                    "source": str(doc.get("source") or ""),
                    "confidence": confidence,
                    "confidence_issue": confidence_issue,
                }
    if not seen_ids:
        raise AssessmentError("标准答案没有有效 ID")
    return standard


@dataclass
class StudentRecord:
    name: str
    source_kind: str
    answers: dict[str, OrderedDict[str, dict[str, Any]]]
    source_order: int


def parse_homework_documents(
    documents: list[dict[str, Any]],
    dimensions: list[str],
    config: dict[str, Any],
    aliases: dict[str, str],
) -> tuple[OrderedDict[str, StudentRecord], list[dict[str, Any]]]:
    students: OrderedDict[str, StudentRecord] = OrderedDict()
    anomalies: list[dict[str, Any]] = []
    order_counter = 0

    for doc in documents:
        headers = doc["headers"]
        id_index = int(resolve_header(headers, config["id_headers"], "ID", True))
        name_index = resolve_header(headers, config["student_name_headers"], "同学名称", False)
        dim_indexes = dimension_indexes(headers, dimensions, config)
        document_name = normalize_name(doc.get("student_name"), aliases)
        if name_index is None and not document_name:
            raise AssessmentError("作业证据必须包含同学名称列，或在顶层提供 student_name")

        for row_number, row in enumerate(doc["rows"], start=2):
            row_id = normalize_id(row[id_index])
            if not row_id:
                if any(compact_text(row[index]) for index in dim_indexes.values()):
                    anomalies.append({"student": document_name, "type": "作业ID为空", "dimension": "", "id": "", "detail": f"第{row_number}行有答案但ID为空"})
                continue
            row_name = normalize_name(row[name_index], aliases) if name_index is not None else document_name
            if document_name and row_name and row_name != document_name:
                raise AssessmentError(f"作业证据 student_name={document_name} 与第{row_number}行姓名={row_name} 冲突")
            name = row_name or document_name
            if not name:
                raise AssessmentError(f"作业第 {row_number} 行 ID {row_id} 没有同学名称")
            if name not in students:
                order_counter += 1
                students[name] = StudentRecord(
                    name=name,
                    source_kind=str(doc.get("source", "docs")),
                    answers={dim: OrderedDict() for dim in dimensions},
                    source_order=order_counter,
                )
            student = students[name]
            confidence, confidence_issue = confidence_for_row(doc, row_id)
            for dimension, index in dim_indexes.items():
                if row_id in student.answers[dimension]:
                    raise AssessmentError(f"学员 {name} 的“{dimension}”出现重复 ID：{row_id}")
                raw = row[index]
                student.answers[dimension][row_id] = {
                    "raw": "" if raw is None else str(raw),
                    "confidence": confidence,
                    "confidence_issue": confidence_issue,
                    "source_path": doc.get("_source_path", ""),
                }
    if not students:
        raise AssessmentError("作业证据没有有效学员数据")
    return students, anomalies


def run_vision(paths: list[Path]) -> list[dict[str, Any]]:
    swift = shutil.which("swift")
    if not swift or sys.platform != "darwin":
        raise AssessmentError("当前环境没有可用的 macOS Vision OCR；请改用 --ocr-engine api")
    started = time.perf_counter()
    command = [swift, str(VISION_SCRIPT), *[str(path) for path in paths]]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssessmentError("Vision OCR 失败：" + (completed.stderr.strip() or "未知错误"))
    try:
        results = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssessmentError("Vision OCR 返回了无效 JSON") from exc
    elapsed = round(time.perf_counter() - started, 3)
    for item in results:
        item.update({"engine": "vision", "batch_elapsed_seconds": elapsed, "workers": 1})
    return results


def ocr_header_matches(value: Any, candidates: list[str]) -> bool:
    normalized = normalize_header(value)
    return any(
        candidate and (candidate == normalized or candidate in normalized or normalized in candidate)
        for candidate in (normalize_header(item) for item in candidates)
    )


def parse_ocr_rows(hits: list[dict[str, Any]], dimension: str, aliases: list[str] | None = None) -> tuple[list[list[Any]], dict[str, float]]:
    def mid_y(hit: dict[str, Any]) -> float:
        return float(hit["y"]) + float(hit["height"]) / 2

    def header_hits(candidates: list[str]) -> list[dict[str, Any]]:
        candidate_set = {normalize_header(value) for value in candidates}
        exact = [hit for hit in hits if normalize_header(hit.get("text")) in candidate_set]
        return exact or [hit for hit in hits if ocr_header_matches(hit.get("text"), candidates)]

    id_headers = header_hits(["ID", "order", "题目ID", "题号"])
    target_headers = header_hits(aliases or [dimension])
    if len(id_headers) != 1 or len(target_headers) != 1:
        raise AssessmentError(f"截图无法唯一定位 ID 和“{dimension}”表头")
    id_header, target = id_headers[0], target_headers[0]
    header_y = max(mid_y(id_header), mid_y(target))
    target_center = float(target["x"]) + float(target["width"]) / 2
    top_right = [
        hit for hit in hits
        if abs(mid_y(hit) - header_y) <= 0.035 and float(hit["x"]) > target_center + 0.03
    ]
    right_boundary = min((target_center + float(hit["x"])) / 2 for hit in top_right) if top_right else min(0.98, target_center + 0.22)
    # Keep the actual ID column. Older logic added 0.02 to the ID header's
    # left edge, which could discard separately recognized ID cells.
    left_boundary = max(
        0.0,
        float(id_header["x"]) - max(0.005, float(id_header.get("width", 0.0)) * 0.25),
    )
    body = [
        hit for hit in hits
        if mid_y(hit) < header_y - 0.025 and left_boundary <= float(hit["x"]) < right_boundary
    ]
    body.sort(key=lambda hit: (-mid_y(hit), float(hit["x"])))

    groups: list[list[dict[str, Any]]] = []
    for hit in body:
        y = mid_y(hit)
        if not groups or abs(y - sum(mid_y(item) for item in groups[-1]) / len(groups[-1])) > 0.018:
            groups.append([hit])
        else:
            groups[-1].append(hit)

    rows: list[list[Any]] = []
    confidences: dict[str, float] = {}
    for group in groups:
        group.sort(key=lambda hit: float(hit["x"]))
        combined = " ".join(str(hit.get("text", "")).strip() for hit in group).strip()
        match = re.search(r"(?<!\d)(\d{1,9})(?!\d)", combined)
        if not match:
            continue
        row_id = normalize_id(match.group(1))
        answer = (combined[: match.start()] + " " + combined[match.end() :]).strip()
        answer = re.sub(r"^[-:：,，;；]+", "", answer).strip()
        rows.append([row_id, answer])
        confidences[row_id] = min(float(hit.get("confidence", 0.0)) for hit in group)
    if not rows:
        raise AssessmentError(f"截图未识别到“{dimension}”的 ID 数据行")
    return rows, confidences


GENERIC_IMAGE_NAME_TOKENS = {
    "截图", "截屏", "屏幕快照", "图片", "照片", "作业", "答案", "考试", "未命名",
    "image", "img", "screenshot", "微信图片",
}


def image_paths(source: Path) -> list[Path]:
    if source.is_file():
        return [source] if source.suffix.lower() in IMAGE_SUFFIXES else []
    if not source.is_dir():
        raise AssessmentError(f"图片路径不存在：{source}")
    return sorted(path for path in source.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)


def load_student_roster(path: Path | None, aliases: dict[str, str]) -> set[str]:
    names = {normalize_name(value, aliases) for value in aliases.values() if normalize_name(value, aliases)}
    if path is None:
        return names
    if not path.exists():
        raise AssessmentError(f"学员名单不存在：{path}")
    if path.suffix.lower() == ".json":
        payload = load_json(path)
        if isinstance(payload, dict):
            values = list(payload.keys()) + list(payload.values())
        elif isinstance(payload, list):
            values = payload
        else:
            raise AssessmentError("学员名单 JSON 必须是姓名数组或姓名映射对象")
    else:
        values = path.read_text(encoding="utf-8").splitlines()
    names.update(normalize_name(value, aliases) for value in values if normalize_name(value, aliases))
    return names


def filename_student(path: Path, aliases: dict[str, str], roster: set[str]) -> str:
    raw = re.sub(r"(?:[-_ ]?(?:副本|copy|\(\d+\)|（\d+）))+$", "", path.stem, flags=re.IGNORECASE).strip()
    candidate = normalize_name(raw, aliases)
    if roster:
        return candidate if candidate in roster else ""
    lowered = candidate.lower()
    if any(token in lowered for token in GENERIC_IMAGE_NAME_TOKENS):
        return ""
    return candidate if re.fullmatch(r"[\u3400-\u9fff·]{2,4}", candidate) else ""


def student_name_from_hits(hits: list[dict[str, Any]], aliases: dict[str, str]) -> str:
    labels = ("同学名称", "同学姓名", "学员姓名", "姓名")
    for hit in hits:
        text = str(hit.get("text") or "").strip()
        match = re.search(r"(?:同学名称|同学姓名|学员姓名|姓名)\s*[:：]?\s*([\u3400-\u9fff·]{2,8})", text)
        if match:
            return normalize_name(match.group(1), aliases)
    for label_hit in hits:
        if normalize_header(label_hit.get("text")) not in {normalize_header(value) for value in labels}:
            continue
        label_y = float(label_hit.get("y", 0)) + float(label_hit.get("height", 0)) / 2
        label_right = float(label_hit.get("x", 0)) + float(label_hit.get("width", 0))
        candidates = [
            hit for hit in hits
            if float(hit.get("x", 0)) >= label_right
            and abs((float(hit.get("y", 0)) + float(hit.get("height", 0)) / 2) - label_y) <= 0.035
            and re.fullmatch(r"[\u3400-\u9fff·]{2,8}", str(hit.get("text") or "").strip())
        ]
        if candidates:
            return normalize_name(min(candidates, key=lambda item: float(item.get("x", 0))).get("text"), aliases)
    return ""


def ocr_documents(
    images_source: Path,
    dimensions: list[str],
    name_aliases: dict[str, str],
    *,
    role: str,
    engine: str = "auto",
    workers: int = 4,
    roster: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    paths = image_paths(images_source)
    if not paths:
        raise AssessmentError(f"图片路径没有支持的图片：{images_source}")
    selected_engine = engine
    if selected_engine == "auto":
        selected_engine = "vision" if sys.platform == "darwin" and shutil.which("swift") else "api"
    started = time.perf_counter()
    if selected_engine == "vision":
        if len(dimensions) != 1:
            raise AssessmentError("macOS Vision 截图解析目前要求考试配置仅含一个评分维度；多维截图请使用 API OCR")
        results = run_vision(paths)
    elif selected_engine == "api":
        try:
            results = run_api_ocr(paths, role=role, dimensions=dimensions, workers=workers)
        except ApiOcrError as exc:
            raise AssessmentError(str(exc)) from exc
    else:
        raise AssessmentError(f"未知 OCR 引擎：{engine}")
    by_path = {str(Path(item["path"]).resolve()): item for item in results}
    documents: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    roster = roster or set()
    config = load_config()
    for path in paths:
        result = by_path.get(str(path.resolve()))
        if not result:
            raise AssessmentError(f"OCR 没有返回图片结果：{path.name}")
        if selected_engine == "vision":
            dimensions_aliases = config.get("header_aliases", {}).get(dimensions[0], [dimensions[0]])
            rows, confidence = parse_ocr_rows(result["hits"], dimensions[0], dimensions_aliases)
            headers = ["ID", dimensions[0]]
            ocr_name = student_name_from_hits(result["hits"], name_aliases) if role == "homework" else ""
        else:
            headers = list(result["headers"])
            rows = list(result["rows"])
            confidence = dict(result.get("confidence") or {})
            # Resolve now so malformed/ambiguous OCR headers fail before scoring.
            resolve_header(headers, config["id_headers"], "ID", True)
            dimension_indexes(headers, dimensions, config)
            ocr_name = normalize_name(result.get("student_name"), name_aliases) if role == "homework" else ""
        name = ""
        if role == "homework":
            file_name = filename_student(path, name_aliases, roster)
            if file_name and ocr_name and file_name != ocr_name:
                raise AssessmentError(f"截图姓名冲突：文件名={file_name}，图中姓名={ocr_name}：{path.name}")
            name = file_name or ocr_name
            if not name:
                raise AssessmentError(f"无法从文件名或截图内容确定学员姓名：{path.name}")
            if roster and name not in roster:
                raise AssessmentError(f"截图识别到的姓名不在学员名单中：{name}：{path.name}")
            if name in seen_names:
                raise AssessmentError(f"截图映射出重复学员：{name}")
            seen_names.add(name)
        document = {
                "schema_version": 1,
                "source": "image_ocr",
                **({"student_name": name} if role == "homework" else {}),
                "sheet": path.name,
                "range": "ID+" + "+".join(dimensions),
                "read_at": "",
                "headers": headers,
                "rows": rows,
                "confidence": confidence,
                "document": {"url": "", "id": path.name, "revision": sha256_file(path)},
                "ocr": {
                    "engine": selected_engine,
                    "elapsed_seconds": result.get("elapsed_seconds"),
                    "batch_elapsed_seconds": result.get("batch_elapsed_seconds"),
                    "workers": result.get("workers", 1),
                },
                "_source_path": str(path),
            }
        normalized_confidence, confidence_issues = normalize_image_confidence(document)
        document["confidence"] = normalized_confidence
        document["confidence_issues"] = confidence_issues
        documents.append(document)
    return documents, {
        "engine": selected_engine,
        "images": len(paths),
        "workers": workers if selected_engine == "api" else 1,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "per_image_seconds": {path.name: by_path[str(path.resolve())].get("elapsed_seconds") for path in paths},
    }


def score_students(
    standard: dict[str, OrderedDict[str, dict[str, Any]]],
    students: OrderedDict[str, StudentRecord],
    dimensions: list[str],
    base_anomalies: list[dict[str, Any]],
    ocr_confidence_threshold: float = 0.75,
) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    anomalies = list(base_anomalies)
    summaries: list[dict[str, Any]] = []

    for student in students.values():
        dim_summaries: dict[str, dict[str, Any]] = {}
        any_pending = False
        for dimension in dimensions:
            expected_map = standard[dimension]
            actual_map = student.answers[dimension]
            correct_count = 0
            wrong_ids: list[str] = []
            review_ids: list[str] = []
            pending = False

            extras = [row_id for row_id in actual_map if row_id not in expected_map]
            for row_id in extras:
                anomalies.append({"student": student.name, "type": "额外ID", "dimension": dimension, "id": row_id, "detail": "不计入分数"})

            for row_id, expected in expected_map.items():
                actual = actual_map.get(row_id)
                expected_raw = expected["raw"]
                expected_keywords: tuple[str, ...] | None = expected["keywords"]
                actual_raw = "" if actual is None else actual["raw"]
                matched_keywords: tuple[str, ...] = ()
                reason = ""
                result = "错误"

                if actual is None:
                    reason = "缺失ID"
                    anomalies.append({"student": student.name, "type": "缺失ID", "dimension": dimension, "id": row_id, "detail": "按未答计错"})
                elif expected.get("confidence_issue"):
                    result = "待复核"
                    reason = "标准答案OCR置信度无效：" + str(expected["confidence_issue"])
                    review_ids.append(row_id)
                    pending = True
                    anomalies.append({"student": student.name, "type": "标准答案OCR置信度无效", "dimension": dimension, "id": row_id, "detail": reason})
                elif expected.get("source") == "image_ocr" and expected.get("confidence") is not None and float(expected["confidence"]) < ocr_confidence_threshold:
                    result = "待复核"
                    reason = f"标准答案OCR置信度 {float(expected['confidence']):.3f} 低于阈值 {ocr_confidence_threshold:.3f}"
                    review_ids.append(row_id)
                    pending = True
                    anomalies.append({"student": student.name, "type": "标准答案OCR低置信度", "dimension": dimension, "id": row_id, "detail": reason})
                elif actual.get("confidence_issue"):
                    result = "待复核"
                    reason = "学员答案OCR置信度无效：" + str(actual["confidence_issue"])
                    review_ids.append(row_id)
                    pending = True
                    anomalies.append({"student": student.name, "type": "学员答案OCR置信度无效", "dimension": dimension, "id": row_id, "detail": reason})
                elif actual.get("confidence") is not None and float(actual["confidence"]) < ocr_confidence_threshold:
                    result = "待复核"
                    reason = f"OCR置信度 {float(actual['confidence']):.3f} 低于阈值 {ocr_confidence_threshold:.3f}"
                    review_ids.append(row_id)
                    pending = True
                    anomalies.append({"student": student.name, "type": "OCR低置信度", "dimension": dimension, "id": row_id, "detail": reason})
                elif expected_keywords is None:
                    if compact_text(actual_raw) == "":
                        result = "正确"
                        reason = "允许空值：空/空"
                    else:
                        result = "错误"
                        reason = "标准空值但作业非空"
                elif compact_text(actual_raw) == "":
                    result = "错误"
                    reason = "作业空值"
                else:
                    matched_keywords = match_standard_keywords(actual_raw, expected_keywords)
                    if matched_keywords:
                        result = "正确"
                        reason = "双向模糊命中标准关键词：" + "｜".join(matched_keywords)
                    else:
                        result = "错误"
                        reason = "作业原文未与任何标准关键词形成包含关系：" + "｜".join(expected_keywords)

                if result == "正确":
                    correct_count += 1
                elif result == "错误":
                    wrong_ids.append(row_id)

                details.append(
                    {
                        "student": student.name,
                        "dimension": dimension,
                        "id": row_id,
                        "standard_raw": expected_raw,
                        "homework_raw": actual_raw,
                        "standard_keywords": "｜".join(expected_keywords) if expected_keywords else "",
                        "matched_keywords": "｜".join(matched_keywords),
                        "result": result,
                        "source": student.source_kind,
                        "standard_confidence": "" if expected.get("confidence") is None else expected.get("confidence"),
                        "confidence": "" if actual is None or actual.get("confidence") is None else actual.get("confidence"),
                        "note": reason,
                    }
                )

            total = len(expected_map)
            accuracy = None if pending else correct_count / total
            dim_summaries[dimension] = {
                "correct": correct_count,
                "total": total,
                "accuracy": accuracy,
                "wrong_ids": wrong_ids,
                "review_ids": review_ids,
                "status": "待复核" if pending else "已完成",
            }
            any_pending = any_pending or pending

        accuracies = [dim_summaries[dimension]["accuracy"] for dimension in dimensions]
        overall = None if any(value is None for value in accuracies) else sum(accuracies) / len(accuracies)
        summaries.append(
            {
                "student": student.name,
                "status": "待复核" if any_pending else "已完成",
                "dimensions": dim_summaries,
                "overall_accuracy": overall,
                "source_order": student.source_order,
            }
        )

    return {"summary": summaries, "details": details, "anomalies": anomalies}


def scoring_fingerprint(
    standard: dict[str, OrderedDict[str, dict[str, Any]]],
    dimensions: list[str],
    profile: dict[str, Any],
    aliases: dict[str, str],
    ocr_confidence_threshold: float,
) -> dict[str, str]:
    standard_payload = {
        dimension: [
            {
                "id": row_id,
                "raw": answer.get("raw_value"),
                "keywords": list(answer.get("keywords") or []),
                "source": answer.get("source"),
                "confidence": answer.get("confidence"),
                "confidence_issue": answer.get("confidence_issue"),
            }
            for row_id, answer in standard[dimension].items()
        ]
        for dimension in dimensions
    }
    return {
        "standard_hash": stable_hash(standard_payload),
        "profile_hash": stable_hash(profile),
        "aliases_hash": stable_hash(aliases),
        "ocr_threshold_hash": stable_hash(ocr_confidence_threshold),
        "scoring_rule_version": SCORING_RULE_VERSION,
    }


def student_fingerprint(student: StudentRecord, anomalies: list[dict[str, Any]]) -> str:
    answers = {
        dimension: [
            {
                "id": row_id,
                "raw": answer.get("raw", ""),
                "confidence": answer.get("confidence"),
                "confidence_issue": answer.get("confidence_issue"),
            }
            for row_id, answer in rows.items()
        ]
        for dimension, rows in student.answers.items()
    }
    relevant_anomalies = [item for item in anomalies if item.get("student") in {"", student.name}]
    return stable_hash({"student": student.name, "source": student.source_kind, "answers": answers, "anomalies": relevant_anomalies})


def score_students_incremental(
    standard: dict[str, OrderedDict[str, dict[str, Any]]],
    students: OrderedDict[str, StudentRecord],
    dimensions: list[str],
    base_anomalies: list[dict[str, Any]],
    profile: dict[str, Any],
    aliases: dict[str, str],
    score_cache_dir: Path | None,
    ocr_confidence_threshold: float = 0.75,
) -> tuple[dict[str, Any], dict[str, int], dict[str, str]]:
    fingerprint = scoring_fingerprint(standard, dimensions, profile, aliases, ocr_confidence_threshold)
    if score_cache_dir is None:
        scored = score_students(standard, students, dimensions, base_anomalies, ocr_confidence_threshold)
        return scored, {"hits": 0, "misses": len(students)}, fingerprint

    score_cache_dir.mkdir(parents=True, exist_ok=True)
    merged = {"summary": [], "details": [], "anomalies": []}
    global_anomalies = [item for item in base_anomalies if not item.get("student")]
    merged["anomalies"].extend(global_anomalies)
    hits = 0
    misses = 0
    for student in students.values():
        relevant_anomalies = [item for item in base_anomalies if item.get("student") == student.name]
        student_hash = student_fingerprint(student, relevant_anomalies)
        cache_key = stable_hash({**fingerprint, "student_hash": student_hash})
        cache_path = score_cache_dir / f"{cache_key}.json"
        fragment: dict[str, Any] | None = None
        if cache_path.exists():
            try:
                fragment = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                fragment = None
        if fragment is None:
            fragment = score_students(
                standard,
                OrderedDict([(student.name, student)]),
                dimensions,
                relevant_anomalies,
                ocr_confidence_threshold,
            )
            atomic_write_json(cache_path, fragment)
            misses += 1
        else:
            hits += 1
        if fragment.get("summary"):
            fragment["summary"][0]["source_order"] = student.source_order
        merged["summary"].extend(fragment.get("summary", []))
        merged["details"].extend(fragment.get("details", []))
        merged["anomalies"].extend(fragment.get("anomalies", []))
    merged["summary"].sort(key=lambda item: item.get("source_order", 0))
    return merged, {"hits": hits, "misses": misses}, fingerprint


def score_fill(rate: float | None) -> str:
    if rate is None:
        return COLORS["pending"]
    percentage = rate * 100
    if percentage >= 90:
        return COLORS["90"]
    if percentage >= 80:
        return COLORS["80"]
    if percentage >= 70:
        return COLORS["70"]
    if percentage >= 60:
        return COLORS["60"]
    return COLORS["low"]


def percent_text(rate: float | None, decimals: int = 0) -> str:
    if rate is None:
        return "待复核"
    value = rate * 100
    if decimals == 0 or abs(value - round(value)) < 1e-9:
        return f"{value:.0f}%"
    return f"{value:.{decimals}f}%"


def find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = []
    explicit = os.environ.get("SCORE_KUAISHOU_FONT")
    if explicit:
        candidates.append(explicit)
    if bold:
        candidates.extend([
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        ])
    candidates.extend([
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ])
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size, index=0)
    raise AssessmentError("找不到可用于PNG的中文字体；请通过 SCORE_KUAISHOU_FONT 指定字体文件")


def wrap_chars(text: str, length: int) -> str:
    text = str(text)
    if len(text) <= length:
        return text
    return "\n".join(text[index : index + length] for index in range(0, len(text), length))


def draw_centered(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, font: ImageFont.FreeTypeFont, fill: str = COLORS["text"]) -> None:
    left, top, right, bottom = box
    lines = str(text).split("\n")
    spacing = 4
    heights = []
    widths = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line or " ", font=font)
        widths.append(bbox[2] - bbox[0])
        heights.append(bbox[3] - bbox[1])
    total_height = sum(heights) + spacing * max(0, len(lines) - 1)
    y = top + (bottom - top - total_height) / 2
    for line, width, height in zip(lines, widths, heights):
        x = left + (right - left - width) / 2
        draw.text((x, y), line, font=font, fill=fill)
        y += height + spacing


def render_png(result: dict[str, Any], output_path: Path) -> None:
    try:
        global Image, ImageDraw, ImageFont
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - environment failure
        raise AssessmentError("缺少 Pillow，无法生成色阶 PNG") from exc
    dimensions: list[str] = result["metadata"]["dimensions"]
    summaries: list[dict[str, Any]] = result["summary"]
    single = len(dimensions) == 1
    title_font = find_font(28, True)
    header_font = find_font(20, True)
    body_font = find_font(20, False)
    strong_font = find_font(21, True)

    title_height, header_height, row_height, legend_height = 70, 72, 58, 76
    margin = 22
    if single:
        dimension = dimensions[0]
        summaries = sorted(summaries, key=lambda item: (item["dimensions"][dimension]["accuracy"] is None, -(item["dimensions"][dimension]["accuracy"] or -1), item["source_order"]))
        widths = [260, 180, 300, 520]
        headers = ["同学", "答对数", f"{dimension}准确率", "错误 ID"]
    else:
        widths = [110, 140, 170] + [190 if len(dim) <= 7 else 220 for dim in dimensions] + [170]
        headers = ["组别", "进度", "姓名"] + [wrap_chars(dim, 7) for dim in dimensions] + [wrap_chars(f"{len(dimensions)}维度平均准确率", 7)]

    width = sum(widths) + margin * 2
    height = margin * 2 + title_height + header_height + row_height * len(summaries) + legend_height
    if width * height > MAX_PNG_PIXELS:
        raise AssessmentError(f"色阶图尺寸过大（{width}×{height}）；请按组拆分后出图，正式JSON和Excel不受影响")
    image = Image.new("RGB", (width, height), "#F4F6F9")
    draw = ImageDraw.Draw(image)
    title = f"{result['metadata']['group']} · {result['metadata']['progress']} 准确率"
    draw_centered(draw, (margin, margin, width - margin, margin + title_height), title, title_font)

    table_top = margin + title_height
    x = margin
    for column, column_width in zip(headers, widths):
        box = (x, table_top, x + column_width, table_top + header_height)
        draw.rectangle(box, fill=COLORS["header"], outline=COLORS["grid"], width=2)
        draw_centered(draw, box, column, header_font)
        x += column_width

    if single:
        dimension = dimensions[0]
        for row_index, summary in enumerate(summaries):
            top = table_top + header_height + row_index * row_height
            dim = summary["dimensions"][dimension]
            wrong = dim["wrong_ids"] + [f"{item}*" for item in dim["review_ids"]]
            values = [summary["student"], f"{dim['correct']}/{dim['total']}" if dim["accuracy"] is not None else "待复核", percent_text(dim["accuracy"], 2), ", ".join(wrong) or "—"]
            fills = [COLORS["body"], COLORS["body"], score_fill(dim["accuracy"]), COLORS["body"]]
            x = margin
            for col_index, (value, column_width, fill) in enumerate(zip(values, widths, fills)):
                box = (x, top, x + column_width, top + row_height)
                draw.rectangle(box, fill=fill, outline=COLORS["grid"], width=2)
                draw_centered(draw, box, value, strong_font if col_index == 2 else body_font, "#FFFFFF" if col_index == 2 and dim["accuracy"] is not None and dim["accuracy"] < 0.6 else COLORS["text"])
                x += column_width
    else:
        data_left = margin + widths[0] + widths[1]
        for row_index, summary in enumerate(summaries):
            top = table_top + header_height + row_index * row_height
            values: list[tuple[str, str]] = [(summary["student"], COLORS["body"])]
            for dimension in dimensions:
                rate = summary["dimensions"][dimension]["accuracy"]
                values.append((percent_text(rate, 2), score_fill(rate)))
            values.append((percent_text(summary["overall_accuracy"], 2), score_fill(summary["overall_accuracy"])))
            x = data_left
            for col_index, ((value, fill), column_width) in enumerate(zip(values, widths[2:])):
                box = (x, top, x + column_width, top + row_height)
                draw.rectangle(box, fill=fill, outline=COLORS["grid"], width=2)
                dark = fill == COLORS["low"]
                draw_centered(draw, box, value, strong_font if col_index > 0 else body_font, "#FFFFFF" if dark else COLORS["text"])
                x += column_width

        body_top = table_top + header_height
        body_bottom = body_top + row_height * len(summaries)
        group_box = (margin, body_top, margin + widths[0], body_bottom)
        progress_box = (margin + widths[0], body_top, margin + widths[0] + widths[1], body_bottom)
        for box, value in [(group_box, result["metadata"]["group"]), (progress_box, result["metadata"]["progress"])]:
            draw.rectangle(box, fill=COLORS["body"], outline="#D0D5DD", width=2)
            draw_centered(draw, box, wrap_chars(value, 6), body_font)

    legend_top = table_top + header_height + row_height * len(summaries) + 10
    legend = [("90–100%", COLORS["90"]), ("80–89%", COLORS["80"]), ("70–79%", COLORS["70"]), ("60–69%", COLORS["60"]), ("<60%", COLORS["low"])]
    slot = (width - 2 * margin) // len(legend)
    for index, (label, color) in enumerate(legend):
        left = margin + index * slot + 18
        draw.rounded_rectangle((left, legend_top + 12, left + 38, legend_top + 50), radius=5, fill=color)
        draw.text((left + 50, legend_top + 17), label, font=body_font, fill=COLORS["text"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\n\r]+", "_", value).strip(" ._")
    return cleaned or "考试"


def build_workbook(result: dict[str, Any], output_path: Path) -> None:
    try:
        from build_workbook import build_workbook as openpyxl_build_workbook
    except ImportError as exc:
        raise AssessmentError("缺少 openpyxl，无法生成Excel") from exc
    try:
        openpyxl_build_workbook(result, output_path)
    except Exception as exc:
        raise AssessmentError(f"Excel生成失败：{exc}") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = AssessmentArgumentParser(description="按固定考试配置核对快手考试准确率")
    parser.add_argument("--manifest", type=Path, help="Manifest JSON；推荐的可续跑入口")
    parser.add_argument("--result-json", type=Path, help="直接从已有评分JSON生成PNG或Excel")
    parser.add_argument("--exam-profile", help="exam_profiles.json 中的考试配置键")
    parser.add_argument("--group", help="组别，例如 29组")
    parser.add_argument("--progress", help="进度/考试名称")
    parser.add_argument("--standard-evidence", type=Path)
    parser.add_argument("--standard-images", type=Path, help="截图考试的标准答案图片或图片文件夹")
    parser.add_argument("--homework-evidence", action="append", default=[], type=Path)
    parser.add_argument("--images", type=Path, help="截图单维考试的个人图片文件夹")
    parser.add_argument("--name-aliases", type=Path, help="姓名别名 JSON")
    parser.add_argument("--student-roster", type=Path, help="截图学员名单（JSON或每行一个姓名的文本）")
    parser.add_argument("--ocr-engine", choices=["auto", "vision", "api"], default="auto")
    parser.add_argument("--ocr-workers", type=int, default=4)
    parser.add_argument("--ocr-confidence-threshold", type=float, default=0.75)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary-only", action="store_true", help="仅保存评分JSON并输出文字摘要")
    parser.add_argument("--png", choices=["on", "off"], default=None)
    parser.add_argument("--xlsx", choices=["auto", "on", "off"], default=None)
    parser.add_argument("--refresh", action="store_true", help="忽略Manifest证据缓存并重新读取")
    parser.add_argument("--skip-xlsx", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def result_filename(group: str, progress: str) -> str:
    return safe_filename(f"{group}_{progress}_评分结果") + ".json"


def save_result(result: dict[str, Any], output_dir: Path) -> Path:
    path = output_dir / result_filename(result["metadata"]["group"], result["metadata"]["progress"])
    atomic_write_json(path, result)
    return path


def normalize_output_settings(args: argparse.Namespace, manifest: dict[str, Any] | None = None) -> tuple[str, str]:
    configured = manifest.get("output", {}) if manifest else {}
    png_mode = args.png or configured.get("png") or "off"
    xlsx_mode = args.xlsx or configured.get("xlsx") or "off"
    if args.skip_xlsx:
        xlsx_mode = "off"
    if args.summary_only:
        return "off", "off"
    return png_mode, xlsx_mode


def output_directory(args: argparse.Namespace, manifest: dict[str, Any] | None = None) -> Path:
    if args.output:
        if not args.output.is_absolute():
            raise AssessmentError(f"--output 必须是绝对交付目录：{args.output}")
        return args.output.resolve()
    if manifest:
        configured = Path(str(manifest["output"].get("dir") or ""))
        if configured.is_absolute():
            return configured
        raise AssessmentError("交付目录必须明确：请使用 --output /绝对路径，或把 Manifest output.dir 配成绝对路径")
    raise AssessmentError("必须使用 --output 提供绝对交付目录")


def ensure_output_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise AssessmentError(f"交付目录必须是绝对路径：{path}")
    if path.exists() and not path.is_dir():
        raise AssessmentError(f"交付目录指向了文件而不是文件夹：{path}")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AssessmentError(f"无法创建交付目录 {path}：{exc}") from exc
    return path


def stopped_items_for(result: dict[str, Any], warnings: list[str] | None = None) -> list[dict[str, str]]:
    """Describe every branch that did not continue to a formal deliverable."""
    stopped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str, str]] = set()

    def add(item: dict[str, Any]) -> None:
        normalized = {
            "stage": str(item.get("stage") or "unknown"),
            "role": str(item.get("role") or ""),
            "student": str(item.get("student") or ""),
            "dimension": str(item.get("dimension") or ""),
            "id": str(item.get("id") or ""),
            "reason": str(item.get("reason") or "未提供停止原因"),
            "next_action": str(item.get("next_action") or "修复原因后重新运行或从同一 task_id 续跑"),
        }
        key = (normalized["stage"], normalized["student"], normalized["dimension"], normalized["id"], normalized["reason"])
        if key not in seen:
            seen.add(key)
            stopped.append(normalized)

    for failed in result.get("failed_documents", []):
        add({
            "stage": "evidence",
            "role": failed.get("role", ""),
            "student": failed.get("student", ""),
            "reason": failed.get("error") or "证据未就绪",
            "next_action": "修复权限、链接、结构或读取失败后，复用同一 task_id 继续运行",
        })
    for detail in result.get("details", []):
        if detail.get("result") == "待复核":
            add({
                "stage": "ocr_review",
                "role": "homework",
                "student": detail.get("student", ""),
                "dimension": detail.get("dimension", ""),
                "id": detail.get("id", ""),
                "reason": detail.get("note") or "OCR结果需要人工复核",
                "next_action": "人工核对该评分格，或提供更清晰截图后重新OCR",
            })
    for warning in warnings or []:
        if warning.startswith(("任务证据不完整", "存在待复核")):
            continue
        add({
            "stage": "output",
            "role": "report",
            "reason": warning,
            "next_action": "修复输出依赖或目录问题后，从正式评分JSON重新生成产物",
        })
    return stopped


def _finite_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


def validate_result_schema(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("schema_version") not in {1, 2, 3}:
        raise AssessmentError("评分JSON必须是 schema_version=1、2或3 的对象")
    if "run_status" not in result:
        raise AssessmentError("评分JSON缺少 run_status，禁止默认视为 complete")
    run_status = result.get("run_status")
    if run_status not in {"complete", "incomplete", "pending_review", "output_failed"}:
        raise AssessmentError("评分JSON run_status 必须是 complete、incomplete、pending_review 或 output_failed")
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        raise AssessmentError("评分JSON缺少 metadata 对象")
    for key in ("exam_profile", "source_mode", "group", "progress", "dimensions"):
        if key not in metadata:
            raise AssessmentError(f"评分JSON metadata 缺少 {key}")
    if metadata["source_mode"] not in {"docs", "images"}:
        raise AssessmentError("评分JSON metadata.source_mode 必须是 docs 或 images")
    if not isinstance(metadata["dimensions"], list) or not metadata["dimensions"]:
        raise AssessmentError("评分JSON metadata.dimensions 必须是非空数组")
    for key in ("summary", "details", "anomalies", "evidence", "standard_answer_evidence", "failed_documents"):
        if not isinstance(result.get(key), list):
            raise AssessmentError(f"评分JSON {key} 必须是数组")
    if not isinstance(result.get("cache_stats"), dict):
        raise AssessmentError("评分JSON cache_stats 必须是对象")

    if run_status in {"complete", "pending_review", "output_failed"} and not result["summary"]:
        raise AssessmentError(f"评分JSON run_status={run_status} 时必须至少包含一名学员")
    if run_status in {"complete", "pending_review", "output_failed"} and (not result["evidence"] or not result["standard_answer_evidence"]):
        raise AssessmentError(f"评分JSON run_status={run_status} 时必须保留完整证据索引和标准答案证据")
    if run_status == "complete" and result["failed_documents"]:
        raise AssessmentError("评分JSON complete 时 failed_documents 必须为空")
    if any(not isinstance(item, dict) or not str(item.get("error") or "").strip() for item in result["failed_documents"]):
        raise AssessmentError("评分JSON failed_documents 每项必须包含明确error")

    dimensions = [str(value) for value in metadata["dimensions"]]
    summaries_by_student: dict[str, dict[str, Any]] = {}
    pending_seen = False
    for index, item in enumerate(result["summary"], start=1):
        if not isinstance(item, dict) or not str(item.get("student") or "").strip() or not isinstance(item.get("dimensions"), dict):
            raise AssessmentError(f"评分JSON summary 第{index}项不完整")
        student = str(item["student"]).strip()
        if student in summaries_by_student:
            raise AssessmentError(f"评分JSON summary 学员重复：{student}")
        summaries_by_student[student] = item
        missing = [dimension for dimension in dimensions if dimension not in item["dimensions"]]
        if missing:
            raise AssessmentError(f"评分JSON summary 第{index}项缺少维度：{', '.join(missing)}")
        accuracies: list[float | None] = []
        student_pending = False
        for dimension in dimensions:
            dim = item["dimensions"][dimension]
            if not isinstance(dim, dict):
                raise AssessmentError(f"评分JSON {student}/{dimension} 必须是对象")
            correct, total, accuracy = dim.get("correct"), dim.get("total"), dim.get("accuracy")
            wrong_ids, review_ids = dim.get("wrong_ids"), dim.get("review_ids")
            if isinstance(correct, bool) or not isinstance(correct, int) or isinstance(total, bool) or not isinstance(total, int) or total <= 0 or not 0 <= correct <= total:
                raise AssessmentError(f"评分JSON {student}/{dimension} 的 correct/total 非法")
            if not isinstance(wrong_ids, list) or not isinstance(review_ids, list):
                raise AssessmentError(f"评分JSON {student}/{dimension} 的 wrong_ids/review_ids 必须是数组")
            wrong = [str(value) for value in wrong_ids]
            review = [str(value) for value in review_ids]
            if len(wrong) != len(set(wrong)) or len(review) != len(set(review)) or set(wrong) & set(review):
                raise AssessmentError(f"评分JSON {student}/{dimension} 的错误ID或复核ID重复/冲突")
            if correct + len(wrong) + len(review) != total:
                raise AssessmentError(f"评分JSON {student}/{dimension} 的正确、错误、复核数量与total不一致")
            if review:
                if accuracy is not None or dim.get("status") != "待复核":
                    raise AssessmentError(f"评分JSON {student}/{dimension} 有review_ids时必须为待复核且accuracy=null")
                student_pending = True
                accuracies.append(None)
            else:
                if not _finite_number(accuracy) or not 0 <= float(accuracy) <= 1 or abs(float(accuracy) - correct / total) > 1e-9:
                    raise AssessmentError(f"评分JSON {student}/{dimension} 的accuracy与correct/total不一致")
                if dim.get("status") != "已完成":
                    raise AssessmentError(f"评分JSON {student}/{dimension} 无复核项时状态必须为已完成")
                accuracies.append(float(accuracy))
        expected_overall = None if student_pending else sum(value for value in accuracies if value is not None) / len(accuracies)
        if expected_overall is None:
            if item.get("overall_accuracy") is not None or item.get("status") != "待复核":
                raise AssessmentError(f"评分JSON {student} 存在复核维度时总状态必须待复核且总准确率为空")
            pending_seen = True
        elif not _finite_number(item.get("overall_accuracy")) or abs(float(item["overall_accuracy"]) - expected_overall) > 1e-9 or item.get("status") != "已完成":
            raise AssessmentError(f"评分JSON {student} 的总准确率或状态不一致")

    detail_counts: dict[tuple[str, str], dict[str, Any]] = {}
    detail_keys: set[tuple[str, str, str]] = set()
    for index, detail in enumerate(result["details"], start=1):
        if not isinstance(detail, dict):
            raise AssessmentError(f"评分JSON details 第{index}项必须是对象")
        student, dimension, row_id = (str(detail.get(key) or "") for key in ("student", "dimension", "id"))
        key = (student, dimension, row_id)
        if not student or dimension not in dimensions or not row_id or key in detail_keys:
            raise AssessmentError(f"评分JSON details 第{index}项身份为空、维度未知或重复")
        if student not in summaries_by_student:
            raise AssessmentError(f"评分JSON details 出现summary中不存在的学员：{student}")
        outcome = detail.get("result")
        if outcome not in {"正确", "错误", "待复核"}:
            raise AssessmentError(f"评分JSON details 第{index}项结果非法")
        detail_keys.add(key)
        counts = detail_counts.setdefault((student, dimension), {"正确": 0, "错误": [], "待复核": []})
        if outcome == "正确":
            counts["正确"] += 1
        else:
            counts[outcome].append(row_id)

    for student, item in summaries_by_student.items():
        for dimension in dimensions:
            dim = item["dimensions"][dimension]
            counts = detail_counts.get((student, dimension), {"正确": 0, "错误": [], "待复核": []})
            if counts["正确"] != dim["correct"] or set(counts["错误"]) != {str(value) for value in dim["wrong_ids"]} or set(counts["待复核"]) != {str(value) for value in dim["review_ids"]}:
                raise AssessmentError(f"评分JSON {student}/{dimension} 的summary与逐题明细不一致")
            if counts["正确"] + len(counts["错误"]) + len(counts["待复核"]) != dim["total"]:
                raise AssessmentError(f"评分JSON {student}/{dimension} 的逐题明细数量与total不一致")

    source_mode = metadata.get("source_mode")
    if source_mode in {"docs", "images"}:
        expected_source = "docs" if source_mode == "docs" else "image_ocr"
        if any(str(item.get("source") or "") != expected_source for item in result["evidence"]):
            raise AssessmentError(f"评分JSON evidence 来源与 source_mode={source_mode} 不一致")
        if any(str(item.get("source") or "") != expected_source for item in result["details"]):
            raise AssessmentError(f"评分JSON details 来源与 source_mode={source_mode} 不一致")
    if run_status == "complete" and pending_seen:
        raise AssessmentError("评分JSON complete 时不能包含待复核学员")
    if run_status == "pending_review" and not pending_seen:
        raise AssessmentError("评分JSON pending_review 时必须包含待复核学员")
    if run_status == "incomplete" and not result["failed_documents"]:
        raise AssessmentError("评分JSON incomplete 时必须说明未完成文档及原因")
    if result.get("stopped_items") is not None:
        if not isinstance(result["stopped_items"], list) or any(not isinstance(item, dict) or not str(item.get("reason") or "").strip() or not str(item.get("next_action") or "").strip() for item in result["stopped_items"]):
            raise AssessmentError("评分JSON stopped_items 必须是含明确reason和next_action的对象数组")
    if run_status != "complete" and not result.get("stopped_items"):
        raise AssessmentError(f"评分JSON {run_status} 时必须在 stopped_items 说明停止原因")
    if run_status == "output_failed" and not any(item.get("stage") == "output" for item in result.get("stopped_items", [])):
        raise AssessmentError("评分JSON output_failed 时必须说明输出阶段的停止原因")
    return result


def emit_optional_outputs(
    result: dict[str, Any],
    output_dir: Path,
    png_mode: str,
    xlsx_mode: str,
) -> tuple[Path | None, Path | None, list[str], bool]:
    png_path: Path | None = None
    xlsx_path: Path | None = None
    warnings: list[str] = []
    hard_output_failure = False
    if result.get("run_status") != "complete":
        message = "存在待复核评分格，已暂停正式PNG和Excel输出" if result.get("run_status") == "pending_review" else "任务证据不完整，已暂停PNG和Excel输出"
        return None, None, [message], False
    stem = safe_filename(f"{result['metadata']['group']}_{result['metadata']['progress']}_准确率")
    if png_mode == "on":
        try:
            png_path = output_dir / f"{stem}_色阶图.png"
            render_png(result, png_path)
        except Exception as exc:
            warnings.append(str(exc))
            hard_output_failure = True
            png_path = None
    if xlsx_mode in {"auto", "on"}:
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            warnings.append("缺少openpyxl，已跳过Excel；评分JSON不受影响")
            hard_output_failure = xlsx_mode == "on"
        else:
            try:
                xlsx_path = output_dir / f"{stem}.xlsx"
                build_workbook(result, xlsx_path)
            except Exception as exc:
                warnings.append(str(exc))
                hard_output_failure = xlsx_mode == "on"
                xlsx_path = None
    return xlsx_path, png_path, warnings, hard_output_failure


def new_run_id(result: dict[str, Any]) -> str:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    entropy = stable_hash({
        "time_ns": time.time_ns(),
        "group": result.get("metadata", {}).get("group"),
        "progress": result.get("metadata", {}).get("progress"),
        "pid": os.getpid(),
    })[:8]
    return f"{timestamp}-{entropy}"


def deliver_result(
    result: dict[str, Any],
    output_root: Path,
    png_mode: str,
    xlsx_mode: str,
) -> tuple[Path | None, Path | None, dict[str, Any], Path, bool]:
    """Publish one isolated run directory only after all requested artifacts finish."""
    output_root = ensure_output_directory(output_root)
    run_id = new_run_id(result)
    result.setdefault("metadata", {})["run_id"] = run_id
    stem = safe_filename(f"{result['metadata']['group']}_{result['metadata']['progress']}")
    final_dir = output_root / f"{stem}__{run_id}"
    stage_dir = Path(tempfile.mkdtemp(prefix=f".{stem}__", dir=output_root))
    try:
        staged_xlsx, staged_png, warnings, hard_failure = emit_optional_outputs(result, stage_dir, png_mode, xlsx_mode)
        if hard_failure:
            for path in (staged_xlsx, staged_png):
                if path is not None:
                    path.unlink(missing_ok=True)
            staged_xlsx = None
            staged_png = None
            result["run_status"] = "output_failed"
        final_xlsx = final_dir / staged_xlsx.name if staged_xlsx else None
        final_png = final_dir / staged_png.name if staged_png else None
        final_json = final_dir / result_filename(result["metadata"]["group"], result["metadata"]["progress"])
        result["output_warnings"] = warnings
        result["stopped_items"] = stopped_items_for(result, warnings)
        result["outputs"] = {
            "json": str(final_json),
            "png": str(final_png) if final_png else None,
            "xlsx": str(final_xlsx) if final_xlsx else None,
        }
        validate_result_schema(result)
        save_result(result, stage_dir)
        os.replace(stage_dir, final_dir)
        return final_xlsx, final_png, result, final_json, hard_failure
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def build_scored_result(
    *,
    profile_key: str,
    group: str,
    progress: str,
    standard_documents: list[dict[str, Any]],
    homework_documents: list[dict[str, Any]],
    aliases: dict[str, str],
    source_mode: str,
    score_cache_dir: Path | None = None,
    failed_documents: list[dict[str, Any]] | None = None,
    ocr_confidence_threshold: float = 0.75,
    ocr_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = load_config()
    profile = config["profiles"].get(profile_key)
    if profile is None:
        raise AssessmentError(f"未知考试配置：{profile_key}；可用值：{', '.join(config['profiles'])}")
    dimensions = list(profile["dimensions"])
    allowed_source = "image_ocr" if source_mode == "images" else "docs"
    if any(document.get("source", "") != allowed_source for document in standard_documents):
        raise AssessmentError(f"考试 {profile['display_name']} 的标准答案必须来自 {allowed_source} 证据")
    if any(document.get("source", "") != allowed_source for document in homework_documents):
        raise AssessmentError(f"考试 {profile['display_name']} 的作业必须来自 {allowed_source} 证据")

    standard = parse_standard(standard_documents, dimensions, config)
    if homework_documents:
        students, initial_anomalies = parse_homework_documents(homework_documents, dimensions, config, aliases)
    else:
        students, initial_anomalies = OrderedDict(), []
    scored, score_cache_stats, fingerprint = score_students_incremental(
        standard,
        students,
        dimensions,
        initial_anomalies,
        profile,
        aliases,
        score_cache_dir,
        ocr_confidence_threshold,
    )
    failures = list(failed_documents or [])
    if not students and not failures:
        failures.append({"role": "homework", "student": "", "error": "没有发现任何有效学员作业"})
    pending_review = any(item.get("status") == "待复核" for item in scored["summary"])
    run_status = "incomplete" if failures else ("pending_review" if pending_review else "complete")
    result = {
        "schema_version": 3,
        "run_status": run_status,
        "metadata": {
            "exam_profile": profile_key,
            "exam_name": profile["display_name"],
            "source_mode": source_mode,
            "group": group,
            "progress": progress,
            "dimensions": dimensions,
            "color_scale": COLORS,
            "ocr_confidence_threshold": ocr_confidence_threshold,
            **({"ocr": ocr_stats} if ocr_stats else {}),
            **fingerprint,
        },
        **scored,
        "evidence": evidence_index(standard_documents + homework_documents),
        "standard_answer_evidence": standard_answer_evidence(standard, dimensions),
        "failed_documents": failures,
        "cache_stats": {"scores": score_cache_stats},
        "output_warnings": [],
    }
    result["stopped_items"] = stopped_items_for(result)
    if len(result["summary"]) != len(students):
        raise AssessmentError("内部校验失败：汇总人数与证据人数不一致")
    validate_result_schema(result)
    return result


def seed_manifest_evidence(manifest: dict[str, Any], refresh: bool) -> None:
    for stage in ("initial", "students"):
        processed: set[str] = set()
        for _ in range(1000):
            state = load_state(manifest)
            specs = document_specs(manifest, state, stage=stage)
            plan = plan_reads(manifest, specs, refresh=refresh)
            readable = {item["item_id"] for item in plan["read"]} - processed
            ingested = 0
            for spec in specs:
                evidence = resolve_path(Path(manifest["_base"]), spec.get("evidence"))
                local_changed = False
                if evidence and evidence.exists() and spec["item_id"] not in processed:
                    try:
                        local_payload = load_json(evidence)
                        stored_hash = str(state.get("documents", {}).get(spec["item_id"], {}).get("content_sha256") or "")
                        local_changed = stable_hash(canonical_evidence(local_payload)) != stored_hash
                    except AssessmentError:
                        local_changed = True
                if evidence and evidence.exists() and (spec["item_id"] in readable or local_changed):
                    ingest_evidence(manifest, spec["item_id"], evidence)
                    processed.add(spec["item_id"])
                    ingested += 1
            if ingested == 0:
                break
        else:
            raise AssessmentError("Manifest 预置证据处理超过安全上限")


def manifest_failures(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    state = load_state(manifest)
    failures: list[dict[str, Any]] = []
    for item_id, entry in state.get("documents", {}).items():
        if entry.get("role") in {"standard", "homework_index", "homework"} and entry.get("status") in INCOMPLETE_DOCUMENT_STATUSES:
            failures.append(
                {
                    "item_id": item_id,
                    "role": entry.get("role", ""),
                    "student": entry.get("student", ""),
                    "error": entry.get("error") or "等待读取",
                    "attempts": entry.get("attempts", 0),
                }
            )
    return failures


def load_named_homework(paths: list[tuple[str, Path]]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for student, path in paths:
        loaded = load_evidence_files([path])
        for document in loaded:
            if not document.get("student_name"):
                document["student_name"] = student
        documents.extend(loaded)
    return documents


def run_image_manifest(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    profile: dict[str, Any],
) -> tuple[Path | None, Path | None, dict[str, Any], Path, bool]:
    base = Path(manifest["_base"])
    aliases = load_aliases(resolve_path(base, manifest.get("name_aliases")))
    standard_config = manifest.get("standard") if isinstance(manifest.get("standard"), dict) else {}
    homework_config = manifest.get("homework") if isinstance(manifest.get("homework"), dict) else {}
    standard_images = resolve_path(base, standard_config.get("images") or standard_config.get("path"))
    standard_evidence = resolve_path(base, standard_config.get("evidence"))
    homework_images = resolve_path(base, homework_config.get("images") or homework_config.get("path"))
    homework_evidence = resolve_path(base, homework_config.get("evidence"))
    roster_path = resolve_path(base, homework_config.get("student_roster") or manifest.get("student_roster"))
    if bool(standard_images) == bool(standard_evidence):
        raise AssessmentError("截图Manifest的 standard 必须且只能配置 images/path 或 evidence")
    if bool(homework_images) == bool(homework_evidence):
        raise AssessmentError("截图Manifest的 homework 必须且只能配置 images/path 或 evidence")
    runtime = manifest["runtime"]
    ocr_stats: dict[str, Any] = {}
    if standard_images:
        standard_documents, ocr_stats["standard"] = ocr_documents(
            standard_images,
            list(profile["dimensions"]),
            aliases,
            role="standard",
            engine=str(runtime["ocr_engine"]),
            workers=int(runtime["ocr_workers"]),
        )
    else:
        standard_documents = load_evidence_files([standard_evidence])
    if homework_images:
        homework_documents, ocr_stats["homework"] = ocr_documents(
            homework_images,
            list(profile["dimensions"]),
            aliases,
            role="homework",
            engine=str(runtime["ocr_engine"]),
            workers=int(runtime["ocr_workers"]),
            roster=load_student_roster(roster_path, aliases),
        )
    else:
        homework_documents = load_evidence_files([homework_evidence])
    result = build_scored_result(
        profile_key=manifest["exam_profile"],
        group=manifest["group"],
        progress=manifest["progress"],
        standard_documents=standard_documents,
        homework_documents=homework_documents,
        aliases=aliases,
        source_mode="images",
        score_cache_dir=cache_root(manifest) / "scores",
        ocr_confidence_threshold=float(runtime["ocr_confidence_threshold"]),
        ocr_stats=ocr_stats or None,
    )
    png_mode, xlsx_mode = normalize_output_settings(args, manifest)
    return deliver_result(result, output_directory(args, manifest), png_mode, xlsx_mode)


def run_manifest(args: argparse.Namespace) -> tuple[Path | None, Path | None, dict[str, Any], Path, bool]:
    manifest = load_manifest(args.manifest.resolve())
    config = load_config()
    profile = config["profiles"].get(manifest["exam_profile"])
    if profile is None:
        raise AssessmentError(f"未知考试配置：{manifest['exam_profile']}")
    if profile["source_mode"] == "images":
        return run_image_manifest(args, manifest, profile)
    seed_manifest_evidence(manifest, args.refresh)
    standard_path, homework_paths, _ = scoring_documents(manifest)
    failures = manifest_failures(manifest)
    if standard_path is None:
        result = {
            "schema_version": 3,
            "run_status": "incomplete",
            "metadata": {
                "exam_profile": manifest["exam_profile"],
                "exam_name": profile["display_name"],
                "source_mode": profile["source_mode"],
                "group": manifest["group"],
                "progress": manifest["progress"],
                "dimensions": profile["dimensions"],
                "color_scale": COLORS,
                "scoring_rule_version": SCORING_RULE_VERSION,
            },
            "summary": [],
            "details": [],
            "anomalies": [],
            "evidence": [],
            "standard_answer_evidence": [],
            "failed_documents": failures or [{"role": "standard", "student": "", "error": "标准答案等待读取"}],
            "cache_stats": {"scores": {"hits": 0, "misses": 0}},
            "output_warnings": ["标准答案未就绪，已保存续跑状态"],
        }
        result["stopped_items"] = stopped_items_for(result)
        png_mode, xlsx_mode = normalize_output_settings(args, manifest)
        return deliver_result(result, output_directory(args, manifest), png_mode, xlsx_mode)

    aliases_path = resolve_path(Path(manifest["_base"]), manifest.get("name_aliases"))
    aliases = load_aliases(aliases_path)
    standard_documents = load_evidence_files([standard_path])
    homework_documents = load_named_homework(homework_paths)
    result = build_scored_result(
        profile_key=manifest["exam_profile"],
        group=manifest["group"],
        progress=manifest["progress"],
        standard_documents=standard_documents,
        homework_documents=homework_documents,
        aliases=aliases,
        source_mode=profile["source_mode"],
        score_cache_dir=cache_root(manifest) / "scores",
        failed_documents=failures,
        ocr_confidence_threshold=float(manifest["runtime"]["ocr_confidence_threshold"]),
    )
    state = load_state(manifest)
    result["cache_stats"]["evidence"] = {
        "cached": sum(1 for item in state.get("documents", {}).values() if item.get("status") == "cached"),
        "success": sum(1 for item in state.get("documents", {}).values() if item.get("status") == "success"),
        "pending_or_failed": len(failures),
    }
    png_mode, xlsx_mode = normalize_output_settings(args, manifest)
    return deliver_result(result, output_directory(args, manifest), png_mode, xlsx_mode)


def run_direct(args: argparse.Namespace) -> tuple[Path | None, Path | None, dict[str, Any], Path, bool]:
    required = [name for name in ("exam_profile", "group", "progress") if not getattr(args, name)]
    if required:
        raise AssessmentError("直接证据模式缺少参数：" + ", ".join("--" + name.replace("_", "-") for name in required))
    if args.output is None:
        raise AssessmentError("直接证据模式必须提供 --output")
    if not 1 <= int(args.ocr_workers) <= 8:
        raise AssessmentError("--ocr-workers 必须在1–8之间")
    if not 0 <= float(args.ocr_confidence_threshold) <= 1:
        raise AssessmentError("--ocr-confidence-threshold 必须在0–1之间")
    config = load_config()
    profile = config["profiles"].get(args.exam_profile)
    if profile is None:
        raise AssessmentError(f"未知考试配置：{args.exam_profile}")
    aliases = load_aliases(args.name_aliases)
    if bool(args.standard_evidence) == bool(args.standard_images):
        raise AssessmentError("必须且只能提供 --standard-evidence 或 --standard-images 之一")
    if args.images and args.homework_evidence:
        raise AssessmentError("--images 与 --homework-evidence 不能同时使用")
    ocr_stats: dict[str, Any] = {}
    if profile["source_mode"] == "images":
        roster = load_student_roster(args.student_roster, aliases)
        if args.standard_images:
            standard_documents, ocr_stats["standard"] = ocr_documents(
                args.standard_images,
                list(profile["dimensions"]),
                aliases,
                role="standard",
                engine=args.ocr_engine,
                workers=args.ocr_workers,
            )
        else:
            standard_documents = load_evidence_files([args.standard_evidence])
        if args.images:
            homework_documents, ocr_stats["homework"] = ocr_documents(
                args.images,
                list(profile["dimensions"]),
                aliases,
                role="homework",
                engine=args.ocr_engine,
                workers=args.ocr_workers,
                roster=roster,
            )
        elif args.homework_evidence:
            homework_documents = load_evidence_files(args.homework_evidence)
        else:
            raise AssessmentError(f"考试 {profile['display_name']} 需要 --images 或 --homework-evidence")
    else:
        if args.standard_images:
            raise AssessmentError(f"考试 {profile['display_name']} 的标准答案必须使用 Docs 结构化证据")
        standard_documents = load_evidence_files([args.standard_evidence])
        if not args.homework_evidence:
            raise AssessmentError(f"考试 {profile['display_name']} 需要至少一个 --homework-evidence")
        homework_documents = load_evidence_files(args.homework_evidence)
    result = build_scored_result(
        profile_key=args.exam_profile,
        group=args.group,
        progress=args.progress,
        standard_documents=standard_documents,
        homework_documents=homework_documents,
        aliases=aliases,
        source_mode=profile["source_mode"],
        ocr_confidence_threshold=float(args.ocr_confidence_threshold),
        ocr_stats=ocr_stats or None,
    )
    png_mode, xlsx_mode = normalize_output_settings(args)
    return deliver_result(result, output_directory(args), png_mode, xlsx_mode)


def run_from_result(args: argparse.Namespace) -> tuple[Path | None, Path | None, dict[str, Any], Path, bool]:
    try:
        result = json.loads(args.result_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssessmentError(f"无法读取评分JSON：{args.result_json}：{exc}") from exc
    result.setdefault("output_warnings", [])
    result.setdefault("stopped_items", stopped_items_for(result, list(result.get("output_warnings") or [])))
    validate_result_schema(result)
    if result["run_status"] == "output_failed":
        # The scoring result is valid; a later rerender may recover after the output dependency is fixed.
        result["run_status"] = "complete"
        result["output_warnings"] = []
        result["stopped_items"] = []
    png_mode, xlsx_mode = normalize_output_settings(args)
    return deliver_result(result, output_directory(args), png_mode, xlsx_mode)


def run(args: argparse.Namespace) -> tuple[Path | None, Path | None, dict[str, Any]]:
    direct_mode = bool(args.standard_evidence or args.standard_images)
    modes = sum(bool(value) for value in (args.manifest, args.result_json, direct_mode))
    if modes != 1:
        raise AssessmentError("必须且只能选择 --manifest、--result-json 或直接输入（--standard-evidence/--standard-images）一种入口")
    if args.manifest:
        xlsx_path, png_path, result, result_path, hard_failure = run_manifest(args)
    elif args.result_json:
        xlsx_path, png_path, result, result_path, hard_failure = run_from_result(args)
    else:
        xlsx_path, png_path, result, result_path, hard_failure = run_direct(args)
    validate_result_schema(result)
    if not result_path.exists():
        raise AssessmentError(f"评分JSON未实际生成：{result_path}")
    for label, path in (("Excel", xlsx_path), ("PNG", png_path)):
        if path is not None and not path.exists():
            raise AssessmentError(f"{label}路径已返回但文件不存在：{path}")
    result["_result_path"] = str(result_path)
    result["_hard_output_failure"] = hard_failure
    return xlsx_path, png_path, result


def summary_text(result: dict[str, Any]) -> str:
    lines = [f"{result['metadata']['group']} {result['metadata']['progress']}：{result['run_status']}"]
    dimensions = result["metadata"].get("dimensions", [])
    for item in result.get("summary", []):
        if len(dimensions) == 1:
            rate = item["dimensions"][dimensions[0]].get("accuracy")
        else:
            rate = item.get("overall_accuracy")
        lines.append(f"{item['student']}：{percent_text(rate, 2)}")
    for stopped in result.get("stopped_items", []):
        target = "/".join(value for value in (stopped.get("student"), stopped.get("dimension"), stopped.get("id")) if value) or stopped.get("role") or stopped.get("stage")
        lines.append(f"未继续：{target}：{stopped.get('reason', '')}；下一步：{stopped.get('next_action', '')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        xlsx_path, png_path, result = run(args)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print(json.dumps({
            "status": "stopped",
            "stopped_items": [{
                "stage": "execution",
                "reason": str(exc),
                "next_action": "按错误信息修复输入、权限、结构或运行环境后重新执行",
            }],
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.summary_only:
        print(summary_text(result))
    pending = sum(1 for item in result["summary"] if item["status"] == "待复核")
    print(
        json.dumps(
            {
                "status": result.get("run_status", "complete"),
                "json": result.get("_result_path"),
                "xlsx": str(xlsx_path) if xlsx_path else None,
                "png": str(png_path) if png_path else None,
                "students": len(result["summary"]),
                "pending_students": pending,
                "anomalies": len(result["anomalies"]),
                "warnings": result.get("output_warnings", []),
                "stopped_items": result.get("stopped_items", []),
            },
            ensure_ascii=False,
        )
    )
    if result.get("run_status") == "output_failed":
        return 3
    if result.get("run_status") == "pending_review":
        return 5
    if result.get("run_status") == "incomplete":
        return 4
    if result.get("_hard_output_failure"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
