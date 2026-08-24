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
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from manifest_runtime import (
    INCOMPLETE_DOCUMENT_STATUSES,
    ManifestError,
    atomic_write_json,
    cache_root,
    document_specs,
    ingest_evidence,
    load_manifest,
    load_state,
    plan_reads,
    resolve_path,
    scoring_documents,
    stable_hash,
)


SKILL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_ROOT / "references" / "exam_profiles.json"
VISION_SCRIPT = SKILL_ROOT / "scripts" / "ocr_vision.swift"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}
SPECIAL_BLANK_DIMS = {"多镜头指令遵循", "多镜头间一致连贯性"}
SCORING_RULE_VERSION = "2026-08-23-bidirectional-keyword-v1"
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


class AssessmentError(RuntimeError):
    pass


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
            if isinstance(row, dict):
                row = [row.get(str(header)) for header in headers]
            if not isinstance(row, list):
                raise AssessmentError(f"证据第 {row_number} 行不是数组或对象：{source_path}")
            if len(row) > len(headers):
                raise AssessmentError(f"证据第 {row_number} 行列数超过表头：{source_path}")
            normalized_rows.append(row + [None] * (len(headers) - len(row)))
        clone = dict(doc)
        clone["headers"] = headers
        clone["rows"] = normalized_rows
        clone["_source_path"] = str(source_path)
        result.append(clone)
    return result


def load_evidence_files(paths: Iterable[Path]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in paths:
        documents.extend(flatten_documents(load_json(path), path))
    return documents


def resolve_header(headers: list[Any], candidates: list[str], label: str, required: bool = True) -> int | None:
    candidate_set = {normalize_header(value) for value in candidates}
    matches = [index for index, value in enumerate(headers) if normalize_header(value) in candidate_set]
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
        public_doc = {key: value for key, value in doc.items() if not key.startswith("_")}
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
            for dimension, index in dim_indexes.items():
                raw = row[index]
                keywords = standard_answer_keywords(raw)
                if keywords is None and dimension not in SPECIAL_BLANK_DIMS:
                    raise AssessmentError(f"标准答案 ID {row_id} 的“{dimension}”为空；该维度不允许标准空值")
                standard[dimension][row_id] = {
                    "raw": display_cell_value(raw),
                    "raw_value": raw,
                    "keywords": keywords,
                    "row_number": row_number,
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
            for dimension, index in dim_indexes.items():
                if row_id in student.answers[dimension]:
                    raise AssessmentError(f"学员 {name} 的“{dimension}”出现重复 ID：{row_id}")
                raw = row[index]
                student.answers[dimension][row_id] = {
                    "raw": "" if raw is None else str(raw),
                    "confidence": doc.get("confidence", {}).get(row_id) if isinstance(doc.get("confidence"), dict) else None,
                    "source_path": doc.get("_source_path", ""),
                }
    if not students:
        raise AssessmentError("作业证据没有有效学员数据")
    return students, anomalies


def run_vision(paths: list[Path]) -> list[dict[str, Any]]:
    swift = shutil.which("swift")
    if not swift or sys.platform != "darwin":
        raise AssessmentError("当前环境没有可用的 macOS Vision OCR；请让 Agent 用原生 OCR 生成作业证据 JSON")
    command = [swift, str(VISION_SCRIPT), *[str(path) for path in paths]]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssessmentError("Vision OCR 失败：" + (completed.stderr.strip() or "未知错误"))
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssessmentError("Vision OCR 返回了无效 JSON") from exc


def parse_ocr_rows(hits: list[dict[str, Any]], dimension: str) -> tuple[list[list[Any]], dict[str, float]]:
    def mid_y(hit: dict[str, Any]) -> float:
        return float(hit["y"]) + float(hit["height"]) / 2

    id_headers = [hit for hit in hits if normalize_header(hit.get("text")) == "ID"]
    target_headers = [hit for hit in hits if normalize_header(hit.get("text")) == normalize_header(dimension)]
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
    left_boundary = max(0.0, float(id_header["x"]) + 0.02)
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


def ocr_documents(images_dir: Path, dimension: str, aliases: dict[str, str]) -> list[dict[str, Any]]:
    paths = sorted(path for path in images_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)
    if not paths:
        raise AssessmentError(f"图片文件夹没有支持的图片：{images_dir}")
    results = run_vision(paths)
    by_path = {str(Path(item["path"]).resolve()): item for item in results}
    documents: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for path in paths:
        name = normalize_name(path.stem, aliases)
        if not name:
            raise AssessmentError(f"图片文件名无法得到学员姓名：{path.name}")
        if name in seen_names:
            raise AssessmentError(f"图片文件名映射出重复学员：{name}")
        seen_names.add(name)
        result = by_path.get(str(path.resolve()))
        if not result:
            raise AssessmentError(f"Vision OCR 没有返回图片结果：{path.name}")
        rows, confidence = parse_ocr_rows(result["hits"], dimension)
        documents.append(
            {
                "schema_version": 1,
                "source": "image_ocr",
                "student_name": name,
                "sheet": path.name,
                "range": "ID+" + dimension,
                "read_at": "",
                "headers": ["ID", dimension],
                "rows": rows,
                "confidence": confidence,
                "document": {"url": "", "id": path.name, "revision": sha256_file(path)},
                "_source_path": str(path),
            }
        )
    return documents


def score_students(
    standard: dict[str, OrderedDict[str, dict[str, Any]]],
    students: OrderedDict[str, StudentRecord],
    dimensions: list[str],
    base_anomalies: list[dict[str, Any]],
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
) -> dict[str, str]:
    standard_payload = {
        dimension: [
            {
                "id": row_id,
                "raw": answer.get("raw_value"),
                "keywords": list(answer.get("keywords") or []),
            }
            for row_id, answer in standard[dimension].items()
        ]
        for dimension in dimensions
    }
    return {
        "standard_hash": stable_hash(standard_payload),
        "profile_hash": stable_hash(profile),
        "aliases_hash": stable_hash(aliases),
        "scoring_rule_version": SCORING_RULE_VERSION,
    }


def student_fingerprint(student: StudentRecord, anomalies: list[dict[str, Any]]) -> str:
    answers = {
        dimension: [
            {
                "id": row_id,
                "raw": answer.get("raw", ""),
                "confidence": answer.get("confidence"),
            }
            for row_id, answer in rows.items()
        ]
        for dimension, rows in student.answers.items()
    }
    relevant_anomalies = [item for item in anomalies if item.get("student") in {"", student.name}]
    return stable_hash({"student": student.name, "answers": answers, "anomalies": relevant_anomalies})


def score_students_incremental(
    standard: dict[str, OrderedDict[str, dict[str, Any]]],
    students: OrderedDict[str, StudentRecord],
    dimensions: list[str],
    base_anomalies: list[dict[str, Any]],
    profile: dict[str, Any],
    aliases: dict[str, str],
    score_cache_dir: Path | None,
) -> tuple[dict[str, Any], dict[str, int], dict[str, str]]:
    fingerprint = scoring_fingerprint(standard, dimensions, profile, aliases)
    if score_cache_dir is None:
        scored = score_students(standard, students, dimensions, base_anomalies)
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
    parser = argparse.ArgumentParser(description="按固定考试配置核对快手考试准确率")
    parser.add_argument("--manifest", type=Path, help="Manifest JSON；推荐的可续跑入口")
    parser.add_argument("--result-json", type=Path, help="直接从已有评分JSON生成PNG或Excel")
    parser.add_argument("--exam-profile", help="exam_profiles.json 中的考试配置键")
    parser.add_argument("--group", help="组别，例如 29组")
    parser.add_argument("--progress", help="进度/考试名称")
    parser.add_argument("--standard-evidence", type=Path)
    parser.add_argument("--homework-evidence", action="append", default=[], type=Path)
    parser.add_argument("--images", type=Path, help="截图单维考试的个人图片文件夹")
    parser.add_argument("--name-aliases", type=Path, help="姓名别名 JSON")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary-only", action="store_true", help="仅保存评分JSON并输出文字摘要")
    parser.add_argument("--png", choices=["on", "off"], default=None)
    parser.add_argument("--xlsx", choices=["auto", "on", "off"], default=None)
    parser.add_argument("--refresh", action="store_true", help="忽略Manifest证据缓存并重新读取")
    parser.add_argument("--overwrite", action="store_true")
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
        return args.output.resolve()
    if manifest:
        return resolve_path(Path(manifest["_base"]), str(manifest["output"]["dir"])) or Path(manifest["_base"]) / "output"
    if args.result_json:
        return args.result_json.resolve().parent
    raise AssessmentError("必须提供 --output，或在Manifest中配置 output.dir")


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
        return None, None, ["任务证据不完整，已暂停PNG和Excel输出"], False
    stem = safe_filename(f"{result['metadata']['group']}_{result['metadata']['progress']}_准确率")
    if png_mode == "on":
        try:
            png_path = output_dir / f"{stem}_色阶图.png"
            render_png(result, png_path)
        except AssessmentError as exc:
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
            except AssessmentError as exc:
                warnings.append(str(exc))
                hard_output_failure = xlsx_mode == "on"
                xlsx_path = None
    return xlsx_path, png_path, warnings, hard_output_failure


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
) -> dict[str, Any]:
    config = load_config()
    profile = config["profiles"].get(profile_key)
    if profile is None:
        raise AssessmentError(f"未知考试配置：{profile_key}；可用值：{', '.join(config['profiles'])}")
    dimensions = list(profile["dimensions"])
    if any(document.get("source", "") != "docs" for document in standard_documents):
        raise AssessmentError("标准答案必须来自 Docs 结构化读取证据，不允许使用截图OCR")
    if source_mode == "docs":
        if any(document.get("source", "") != "docs" for document in homework_documents):
            raise AssessmentError(f"考试 {profile['display_name']} 的作业必须来自 Docs 结构化读取证据")

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
    )
    failures = list(failed_documents or [])
    result = {
        "schema_version": 2,
        "run_status": "incomplete" if failures else "complete",
        "metadata": {
            "exam_profile": profile_key,
            "exam_name": profile["display_name"],
            "source_mode": source_mode,
            "group": group,
            "progress": progress,
            "dimensions": dimensions,
            "color_scale": COLORS,
            **fingerprint,
        },
        **scored,
        "evidence": evidence_index(standard_documents + homework_documents),
        "standard_answer_evidence": standard_answer_evidence(standard, dimensions),
        "failed_documents": failures,
        "cache_stats": {"scores": score_cache_stats},
        "output_warnings": [],
    }
    if len(result["summary"]) != len(students):
        raise AssessmentError("内部校验失败：汇总人数与证据人数不一致")
    return result


def seed_manifest_evidence(manifest: dict[str, Any], refresh: bool) -> None:
    for stage in ("initial", "students"):
        state = load_state(manifest)
        specs = document_specs(manifest, state, stage=stage)
        plan_reads(manifest, specs, refresh=refresh)
        for spec in specs:
            evidence = resolve_path(Path(manifest["_base"]), spec.get("evidence"))
            if evidence and evidence.exists():
                ingest_evidence(manifest, spec["item_id"], evidence)


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


def run_manifest(args: argparse.Namespace) -> tuple[Path | None, Path | None, dict[str, Any], Path, bool]:
    manifest = load_manifest(args.manifest.resolve())
    seed_manifest_evidence(manifest, args.refresh)
    standard_path, homework_paths, _ = scoring_documents(manifest)
    failures = manifest_failures(manifest)
    if standard_path is None:
        output_dir = output_directory(args, manifest)
        output_dir.mkdir(parents=True, exist_ok=True)
        config = load_config()
        profile = config["profiles"].get(manifest["exam_profile"])
        if profile is None:
            raise AssessmentError(f"未知考试配置：{manifest['exam_profile']}")
        result = {
            "schema_version": 2,
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
        result_path = save_result(result, output_dir)
        return None, None, result, result_path, False

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
        source_mode="docs",
        score_cache_dir=cache_root(manifest) / "scores",
        failed_documents=failures,
    )
    state = load_state(manifest)
    result["cache_stats"]["evidence"] = {
        "cached": sum(1 for item in state.get("documents", {}).values() if item.get("status") == "cached"),
        "success": sum(1 for item in state.get("documents", {}).values() if item.get("status") == "success"),
        "pending_or_failed": len(failures),
    }
    output_dir = output_directory(args, manifest)
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = save_result(result, output_dir)
    png_mode, xlsx_mode = normalize_output_settings(args, manifest)
    xlsx_path, png_path, warnings, hard_failure = emit_optional_outputs(result, output_dir, png_mode, xlsx_mode)
    result["output_warnings"] = warnings
    result["outputs"] = {"json": str(result_path), "png": str(png_path) if png_path else None, "xlsx": str(xlsx_path) if xlsx_path else None}
    save_result(result, output_dir)
    return xlsx_path, png_path, result, result_path, hard_failure


def run_direct(args: argparse.Namespace) -> tuple[Path | None, Path | None, dict[str, Any], Path, bool]:
    required = [name for name in ("exam_profile", "group", "progress", "standard_evidence") if not getattr(args, name)]
    if required:
        raise AssessmentError("直接证据模式缺少参数：" + ", ".join("--" + name.replace("_", "-") for name in required))
    if args.output is None:
        raise AssessmentError("直接证据模式必须提供 --output")
    config = load_config()
    profile = config["profiles"].get(args.exam_profile)
    if profile is None:
        raise AssessmentError(f"未知考试配置：{args.exam_profile}")
    aliases = load_aliases(args.name_aliases)
    standard_documents = load_evidence_files([args.standard_evidence])
    if args.images and args.homework_evidence:
        raise AssessmentError("--images 与 --homework-evidence 不能同时使用")
    if profile["source_mode"] == "images":
        if not args.images:
            raise AssessmentError(f"考试 {profile['display_name']} 需要 --images 图片文件夹")
        homework_documents = ocr_documents(args.images, profile["dimensions"][0], aliases)
    else:
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
    )
    output_dir = output_directory(args)
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = save_result(result, output_dir)
    png_mode, xlsx_mode = normalize_output_settings(args)
    xlsx_path, png_path, warnings, hard_failure = emit_optional_outputs(result, output_dir, png_mode, xlsx_mode)
    result["output_warnings"] = warnings
    result["outputs"] = {"json": str(result_path), "png": str(png_path) if png_path else None, "xlsx": str(xlsx_path) if xlsx_path else None}
    save_result(result, output_dir)
    return xlsx_path, png_path, result, result_path, hard_failure


def run_from_result(args: argparse.Namespace) -> tuple[Path | None, Path | None, dict[str, Any], Path, bool]:
    try:
        result = json.loads(args.result_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssessmentError(f"无法读取评分JSON：{args.result_json}：{exc}") from exc
    if result.get("schema_version") not in {1, 2}:
        raise AssessmentError("评分JSON schema_version 必须为1或2")
    result.setdefault("run_status", "complete")
    result.setdefault("output_warnings", [])
    output_dir = output_directory(args)
    output_dir.mkdir(parents=True, exist_ok=True)
    png_mode, xlsx_mode = normalize_output_settings(args)
    xlsx_path, png_path, warnings, hard_failure = emit_optional_outputs(result, output_dir, png_mode, xlsx_mode)
    result["output_warnings"] = warnings
    result_path = save_result(result, output_dir)
    result["outputs"] = {"json": str(result_path), "png": str(png_path) if png_path else None, "xlsx": str(xlsx_path) if xlsx_path else None}
    save_result(result, output_dir)
    return xlsx_path, png_path, result, result_path, hard_failure


def run(args: argparse.Namespace) -> tuple[Path | None, Path | None, dict[str, Any]]:
    modes = sum(bool(value) for value in (args.manifest, args.result_json, args.standard_evidence))
    if modes != 1:
        raise AssessmentError("必须且只能选择 --manifest、--result-json 或 --standard-evidence 一种入口")
    if args.manifest:
        xlsx_path, png_path, result, result_path, hard_failure = run_manifest(args)
    elif args.result_json:
        xlsx_path, png_path, result, result_path, hard_failure = run_from_result(args)
    else:
        xlsx_path, png_path, result, result_path, hard_failure = run_direct(args)
    result["_result_path"] = str(result_path)
    result["_hard_output_failure"] = hard_failure
    return xlsx_path, png_path, result


def summary_text(result: dict[str, Any]) -> str:
    lines = [f"{result['metadata']['group']} {result['metadata']['progress']}：{result.get('run_status', 'complete')}"]
    dimensions = result["metadata"].get("dimensions", [])
    for item in result.get("summary", []):
        if len(dimensions) == 1:
            rate = item["dimensions"][dimensions[0]].get("accuracy")
        else:
            rate = item.get("overall_accuracy")
        lines.append(f"{item['student']}：{percent_text(rate, 2)}")
    for failed in result.get("failed_documents", []):
        lines.append(f"读取失败/待续跑：{failed.get('student') or failed.get('role')}：{failed.get('error', '')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        xlsx_path, png_path, result = run(args)
    except (AssessmentError, ManifestError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
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
            },
            ensure_ascii=False,
        )
    )
    if result.get("run_status") != "complete":
        return 4
    if result.get("_hard_output_failure"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
