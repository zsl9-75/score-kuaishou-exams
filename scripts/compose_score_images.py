#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MAX_PNG_PIXELS = 80_000_000
SUMMARY_LABELS = ("总准确率", "平均准确率", "综合准确率")
COLORS = {
    "background": "#F5F7FA",
    "body": "#FAFBFD",
    "header": "#DCE8F7",
    "grid": "#FFFFFF",
    "divider": "#C8D2DF",
    "text": "#202A36",
    "muted": "#607086",
    "missing": "#E9EDF2",
    "90": "#6AB37B",
    "80": "#A7D08D",
    "70": "#FEE07B",
    "60": "#F5A05C",
    "low": "#F35161",
}


class CompositionError(RuntimeError):
    pass


class CompositionArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CompositionError(f"命令参数错误：{message}")


def compact_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[\s\u3000]+", "", str(value).strip())


def display_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not compact_text(text):
        raise CompositionError(f"{label}不能为空")
    return text


@dataclass(frozen=True)
class PercentValue:
    number: float
    text: str


def parse_percent(value: Any, label: str, *, allow_missing: bool = False) -> PercentValue | None:
    if value is None or (isinstance(value, str) and compact_text(value) in {"", "/", "／", "—", "-"}):
        if allow_missing:
            return None
        raise CompositionError(f"{label}缺少百分比")
    if isinstance(value, bool):
        raise CompositionError(f"{label}必须是0–100百分比")
    display = str(value).strip().replace("％", "%")
    raw = display[:-1].strip() if display.endswith("%") else display
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise CompositionError(f"{label}不是合法百分比：{value!r}") from exc
    if not math.isfinite(number) or not 0 <= number <= 100:
        raise CompositionError(f"{label}必须在0–100之间：{value!r}")
    if not display.endswith("%"):
        display += "%"
    return PercentValue(number, display)


@dataclass
class ScoreRow:
    student: str
    scores: OrderedDict[str, PercentValue]
    overall: PercentValue | None
    source_order: int


@dataclass
class Source:
    label: str
    label_key: str
    group: str
    progress: str
    image: Path
    dimensions: list[str]
    dimension_labels: dict[str, str]
    rows: list[ScoreRow]
    source_order: int


@dataclass
class MergedRow:
    group: str
    progress: str
    student: str
    scores: OrderedDict[str, PercentValue]
    overall: PercentValue | None
    row_order: int


@dataclass
class Category:
    label: str
    label_key: str
    dimensions: list[str] = field(default_factory=list)
    dimension_labels: dict[str, str] = field(default_factory=dict)
    rows: OrderedDict[tuple[str, str], MergedRow] = field(default_factory=OrderedDict)
    source_order: int = 0


def load_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CompositionError(f"找不到composition JSON：{path}") from exc
    except json.JSONDecodeError as exc:
        raise CompositionError(f"composition JSON无效：{exc}") from exc
    if not isinstance(payload, dict):
        raise CompositionError("composition JSON顶层必须是对象")
    if payload.get("schema_version") != 1:
        raise CompositionError("composition JSON schema_version必须为1")
    return payload


def load_source(raw: Any, index: int, root_group: str, root_progress: str) -> Source:
    if not isinstance(raw, dict):
        raise CompositionError(f"sources[{index}]必须是对象")
    label = display_text(raw.get("label"), f"sources[{index}].label")
    label_key = compact_text(label)
    group = display_text(raw.get("group") or root_group, f"sources[{index}].group")
    progress = display_text(raw.get("progress") or root_progress, f"sources[{index}].progress")
    image_value = display_text(raw.get("image"), f"sources[{index}].image")
    image = Path(image_value)
    if not image.is_absolute():
        raise CompositionError(f"sources[{index}].image必须是绝对路径：{image}")
    if not image.is_file():
        raise CompositionError(f"sources[{index}].image不存在：{image}")

    raw_dimensions = raw.get("dimensions")
    if not isinstance(raw_dimensions, list) or not raw_dimensions:
        raise CompositionError(f"sources[{index}].dimensions必须是非空数组")
    dimensions: list[str] = []
    dimension_labels: dict[str, str] = {}
    for dim_index, raw_dimension in enumerate(raw_dimensions):
        label_text = display_text(raw_dimension, f"sources[{index}].dimensions[{dim_index}]")
        key = compact_text(label_text)
        if key in dimension_labels:
            raise CompositionError(f"sources[{index}]规范化后维度重复：{label_text}")
        if any(token in key for token in SUMMARY_LABELS):
            raise CompositionError(f"sources[{index}]汇总列不能放入dimensions：{label_text}")
        dimensions.append(key)
        dimension_labels[key] = label_text

    raw_rows = raw.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise CompositionError(f"sources[{index}].rows必须是非空数组")
    rows: list[ScoreRow] = []
    seen_students: set[str] = set()
    for row_index, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, dict):
            raise CompositionError(f"sources[{index}].rows[{row_index}]必须是对象")
        student = compact_text(raw_row.get("student"))
        if not student:
            raise CompositionError(f"sources[{index}].rows[{row_index}].student不能为空")
        if student in seen_students:
            raise CompositionError(f"sources[{index}]出现重复姓名：{student}")
        seen_students.add(student)
        raw_scores = raw_row.get("scores")
        if not isinstance(raw_scores, dict):
            raise CompositionError(f"sources[{index}].rows[{row_index}].scores必须是对象")
        normalized_scores: dict[str, Any] = {}
        for raw_key, raw_value in raw_scores.items():
            key = compact_text(raw_key)
            if not key or key in normalized_scores:
                raise CompositionError(f"sources[{index}] {student} 的成绩表头为空或重复")
            normalized_scores[key] = raw_value
        missing = [dimension_labels[key] for key in dimensions if key not in normalized_scores]
        extra = [key for key in normalized_scores if key not in dimension_labels]
        if missing or extra:
            raise CompositionError(f"sources[{index}] {student} 的成绩列不一致：缺失={missing}，额外={extra}")
        scores: OrderedDict[str, PercentValue] = OrderedDict()
        for key in dimensions:
            scores[key] = parse_percent(
                normalized_scores[key],
                f"sources[{index}] {student}/{dimension_labels[key]}",
                allow_missing=False,
            )
        overall = parse_percent(raw_row.get("overall"), f"sources[{index}] {student}/总准确率", allow_missing=True)
        rows.append(ScoreRow(student, scores, overall, row_index))
    return Source(label, label_key, group, progress, image.resolve(), dimensions, dimension_labels, rows, index)


def merge_categories(sources: list[Source]) -> OrderedDict[str, Category]:
    categories: OrderedDict[str, Category] = OrderedDict()
    row_counter = 0
    for source in sources:
        category = categories.get(source.label_key)
        if category is None:
            category = Category(source.label, source.label_key, source_order=source.source_order)
            categories[source.label_key] = category
        for dimension in source.dimensions:
            if dimension not in category.dimension_labels:
                category.dimensions.append(dimension)
                category.dimension_labels[dimension] = source.dimension_labels[dimension]
        for source_row in source.rows:
            key = (compact_text(source.group), source_row.student)
            existing = category.rows.get(key)
            if existing is None:
                existing = MergedRow(
                    source.group,
                    source.progress,
                    source_row.student,
                    OrderedDict(source_row.scores),
                    source_row.overall,
                    row_counter,
                )
                row_counter += 1
                category.rows[key] = existing
                continue
            if compact_text(existing.progress) != compact_text(source.progress):
                raise CompositionError(f"{category.label}/{source.group}/{source_row.student} 的进度冲突")
            for dimension, value in source_row.scores.items():
                old = existing.scores.get(dimension)
                if old is not None and value is not None and abs(old.number - value.number) > 1e-9:
                    label = category.dimension_labels[dimension]
                    raise CompositionError(f"{category.label}/{source.group}/{source_row.student}/{label} 出现冲突成绩")
                if dimension not in existing.scores or existing.scores[dimension] is None:
                    existing.scores[dimension] = value
            if existing.overall is not None and source_row.overall is not None and abs(existing.overall.number - source_row.overall.number) > 1e-9:
                raise CompositionError(f"{category.label}/{source.group}/{source_row.student} 出现冲突总准确率")
            if existing.overall is None:
                existing.overall = source_row.overall
    return categories


def ordered_keys(
    payload: dict[str, Any],
    categories: OrderedDict[str, Category],
    *,
    allow_missing: bool = False,
) -> list[str]:
    raw_order = payload.get("category_order")
    if raw_order is not None:
        if not isinstance(raw_order, list) or len({compact_text(value) for value in raw_order}) != len(raw_order):
            raise CompositionError("category_order必须是无重复数组")
        requested = [compact_text(value) for value in raw_order]
        unknown = [value for value in requested if value not in categories]
        if unknown and not allow_missing:
            raise CompositionError(f"category_order包含未知考核类型：{unknown}")
        present = [value for value in requested if value in categories]
        return present + [key for key in categories if key not in present]

    def priority(key: str) -> tuple[int, int]:
        if "文生" in key:
            return (0, categories[key].source_order)
        if "图生" in key:
            return (1, categories[key].source_order)
        return (2, categories[key].source_order)

    return sorted(categories, key=priority)


def first_seen_groups(sources: list[Source], requested: Any) -> list[str]:
    labels: OrderedDict[str, str] = OrderedDict()
    for source in sources:
        labels.setdefault(compact_text(source.group), source.group)
    if requested is None:
        return list(labels)
    if not isinstance(requested, list) or len({compact_text(value) for value in requested}) != len(requested):
        raise CompositionError("group_order必须是无重复数组")
    result = [compact_text(value) for value in requested]
    unknown = [value for value in result if value not in labels]
    if unknown:
        raise CompositionError(f"group_order包含未知组别：{unknown}")
    return result + [key for key in labels if key not in result]


def category_identity(category: Category, group_key: str) -> set[str]:
    return {row.student for (group, _), row in category.rows.items() if group == group_key}


def validate_append_references(payload: dict[str, Any], categories: OrderedDict[str, Category]) -> None:
    raw_appends = payload.get("append_metrics") or []
    if not isinstance(raw_appends, list):
        raise CompositionError("append_metrics必须是数组")
    output_keys: set[str] = set()
    for index, raw in enumerate(raw_appends):
        if not isinstance(raw, dict):
            raise CompositionError(f"append_metrics[{index}]必须是对象")
        source_key = compact_text(raw.get("source_label"))
        target_key = compact_text(raw.get("target_label"))
        dimension_key = compact_text(raw.get("dimension"))
        output_label = display_text(raw.get("output_label") or raw.get("dimension"), f"append_metrics[{index}].output_label")
        output_key = compact_text(output_label)
        if source_key not in categories or target_key not in categories:
            raise CompositionError(f"append_metrics[{index}]引用了未知考核类型")
        if source_key == target_key:
            raise CompositionError(f"append_metrics[{index}]来源与目标不能相同")
        if dimension_key not in categories[source_key].dimension_labels:
            raise CompositionError(f"append_metrics[{index}]找不到来源维度：{raw.get('dimension')}")
        if output_key in output_keys:
            raise CompositionError(f"append_metrics输出列重复：{output_label}")
        output_keys.add(output_key)


def structure_digest(structure: dict[str, Any]) -> str:
    encoded = json.dumps(structure, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _build_model_from_sources(
    payload: dict[str, Any],
    sources: list[Source],
    group_keys: list[str],
) -> dict[str, Any]:
    title = display_text(payload.get("title"), "title")
    categories = merge_categories(sources)
    category_keys = ordered_keys(payload, categories, allow_missing=True)

    raw_appends = payload.get("append_metrics") or []
    supplements: list[dict[str, Any]] = []
    consumed: set[str] = set()
    supplement_values: dict[tuple[str, str, str, str], PercentValue] = {}
    supplement_labels: set[str] = set()
    for index, raw in enumerate(raw_appends):
        if not isinstance(raw, dict):
            raise CompositionError(f"append_metrics[{index}]必须是对象")
        source_key = compact_text(raw.get("source_label"))
        target_key = compact_text(raw.get("target_label"))
        dimension_key = compact_text(raw.get("dimension"))
        output_label = display_text(raw.get("output_label") or raw.get("dimension"), f"append_metrics[{index}].output_label")
        output_key = compact_text(output_label)
        if source_key not in categories or target_key not in categories:
            continue
        source_category = categories[source_key]
        target_category = categories[target_key]
        if dimension_key not in source_category.dimension_labels:
            continue
        if output_key in supplement_labels:
            raise CompositionError(f"append_metrics输出列重复：{output_label}")
        supplement_labels.add(output_key)
        for group_key in group_keys:
            source_names = category_identity(source_category, group_key)
            target_names = category_identity(target_category, group_key)
            if source_names != target_names:
                raise CompositionError(
                    f"追加指标姓名未对齐：{source_category.label}->{target_category.label}/{group_key}，"
                    f"来源独有={sorted(source_names-target_names)}，目标独有={sorted(target_names-source_names)}"
                )
            for student in target_names:
                value = source_category.rows[(group_key, student)].scores.get(dimension_key)
                if value is None:
                    raise CompositionError(f"追加指标缺少成绩：{group_key}/{student}/{output_label}")
                supplement_values[(target_key, group_key, student, output_key)] = value
        keep_source = bool(raw.get("keep_source", False))
        if not keep_source:
            consumed.add(source_key)
        supplements.append({
            "source_key": source_key,
            "target_key": target_key,
            "dimension_key": dimension_key,
            "key": output_key,
            "label": output_label,
        })

    rendered_keys = [key for key in category_keys if key not in consumed]
    if not rendered_keys:
        raise CompositionError("append_metrics移除了全部可渲染考核类型")
    for group_key in group_keys:
        expected: set[str] | None = None
        expected_label = ""
        for key in rendered_keys:
            names = category_identity(categories[key], group_key)
            if not names:
                raise CompositionError(f"组别{group_key}缺少考核类型：{categories[key].label}")
            if expected is None:
                expected, expected_label = names, categories[key].label
            elif names != expected:
                raise CompositionError(
                    f"跨考核姓名未对齐：{group_key}/{expected_label}与{categories[key].label}，"
                    f"前者独有={sorted(expected-names)}，后者独有={sorted(names-expected)}"
                )

    global_dimensions: list[str] = []
    dimension_labels: dict[str, str] = {}
    for key in rendered_keys:
        category = categories[key]
        for dimension in category.dimensions:
            if dimension not in dimension_labels:
                global_dimensions.append(dimension)
                dimension_labels[dimension] = category.dimension_labels[dimension]
    collisions = [item["label"] for item in supplements if item["key"] in dimension_labels]
    if collisions:
        raise CompositionError(f"追加指标与已有维度重名：{collisions}")

    canonical_order: dict[str, list[str]] = {}
    anchor_category = categories[rendered_keys[0]]
    for group_key in group_keys:
        anchor_rows = [row for (group, _), row in anchor_category.rows.items() if group == group_key]
        anchor_rows.sort(key=lambda item: item.row_order)
        canonical_order[group_key] = [row.student for row in anchor_rows]

    rows: list[dict[str, Any]] = []
    for category_key in rendered_keys:
        category = categories[category_key]
        for group_key in group_keys:
            group_rows = [row for (group, _), row in category.rows.items() if group == group_key]
            order = {student: index for index, student in enumerate(canonical_order[group_key])}
            group_rows.sort(key=lambda item: order[item.student])
            for row in group_rows:
                values = [row.scores.get(dimension) for dimension in global_dimensions]
                appended = [
                    supplement_values.get((category_key, group_key, row.student, item["key"]))
                    if item["target_key"] == category_key else None
                    for item in supplements
                ]
                rows.append({
                    "group": row.group,
                    "progress": row.progress,
                    "category": category.label,
                    "student": row.student,
                    "values": values,
                    "overall": row.overall,
                    "supplements": appended,
                })
    summary_label = display_text(payload.get("summary_label") or "总准确率", "summary_label")
    structure = {
        "categories": [
            {
                "label": key,
                "dimensions": categories[key].dimensions,
            }
            for key in rendered_keys
        ],
        "summary_label": compact_text(summary_label),
        "supplements": [
            {
                "source": item["source_key"],
                "target": item["target_key"],
                "dimension": item["dimension_key"],
                "output": item["key"],
            }
            for item in supplements
        ],
    }
    signature = structure_digest(structure)
    group_labels: list[str] = []
    for group_key in group_keys:
        matching = next((source.group for source in sources if compact_text(source.group) == group_key), group_key)
        group_labels.append(matching)
    return {
        "title": title,
        "dimensions": [dimension_labels[key] for key in global_dimensions],
        "summary_label": summary_label,
        "supplement_labels": [item["label"] for item in supplements],
        "rows": rows,
        "groups": group_labels,
        "structure": structure,
        "structure_signature": signature,
    }


def build_models(payload: dict[str, Any]) -> list[dict[str, Any]]:
    display_text(payload.get("title"), "title")
    layout = str(payload.get("layout") or "auto").strip()
    if layout not in {"auto", "stack_by_assessment"}:
        raise CompositionError("layout只支持auto或stack_by_assessment")
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise CompositionError("sources必须是非空数组")
    root_group = str(payload.get("group") or "").strip()
    root_progress = str(payload.get("progress") or "").strip()
    sources = [load_source(raw, index, root_group, root_progress) for index, raw in enumerate(raw_sources)]
    all_categories = merge_categories(sources)
    ordered_keys(payload, all_categories)
    validate_append_references(payload, all_categories)
    group_keys = first_seen_groups(sources, payload.get("group_order"))

    buckets: OrderedDict[str, list[str]] = OrderedDict()
    for group_key in group_keys:
        group_sources = [source for source in sources if compact_text(source.group) == group_key]
        model = _build_model_from_sources(payload, group_sources, [group_key])
        buckets.setdefault(model["structure_signature"], []).append(group_key)

    if layout == "stack_by_assessment" and len(buckets) > 1:
        raise CompositionError("显式stack_by_assessment要求所有组的考试结构一致")

    models: list[dict[str, Any]] = []
    for signature, bucket_groups in buckets.items():
        bucket_sources = [source for source in sources if compact_text(source.group) in set(bucket_groups)]
        model = _build_model_from_sources(payload, bucket_sources, bucket_groups)
        if model["structure_signature"] != signature:
            raise CompositionError("自动分组后考试结构不稳定，请复核考核类型与维度")
        models.append(model)
    return models


def build_model(payload: dict[str, Any]) -> dict[str, Any]:
    models = build_models(payload)
    if len(models) != 1:
        details = [f"{','.join(model['groups'])}:{model['structure_signature']}" for model in models]
        raise CompositionError(f"检测到多种考试结构，请使用--output-dir自动分图：{details}")
    return models[0]


def find_font(size: int, bold: bool = False):
    try:
        from PIL import ImageFont
    except ImportError as exc:
        raise CompositionError("缺少Pillow，无法生成合并成绩图") from exc
    explicit = os.environ.get("SCORE_KUAISHOU_FONT")
    candidates = [explicit] if explicit else []
    if bold:
        candidates.extend([
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        ])
    candidates.extend([
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ])
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size, index=0)
    raise CompositionError("找不到可用于合图的中文字体；请通过SCORE_KUAISHOU_FONT指定字体")


def wrap_header(text: str, length: int = 7) -> str:
    compact = compact_text(text)
    if len(compact) <= length:
        return compact
    return "\n".join(compact[index:index + length] for index in range(0, len(compact), length))


def centered(draw: Any, box: tuple[int, int, int, int], text: str, used_font: Any, color: str = COLORS["text"]) -> None:
    lines = str(text).split("\n")
    spacing = 4
    boxes = [draw.textbbox((0, 0), line or " ", font=used_font) for line in lines]
    heights = [item[3] - item[1] for item in boxes]
    total = sum(heights) + spacing * max(0, len(lines) - 1)
    y = box[1] + (box[3] - box[1] - total) / 2
    for line, measured, height in zip(lines, boxes, heights):
        width = measured[2] - measured[0]
        x = box[0] + (box[2] - box[0] - width) / 2
        draw.text((x, y), line, font=used_font, fill=color)
        y += height + spacing


def score_fill(value: PercentValue | None) -> str:
    if value is None:
        return COLORS["missing"]
    if value.number >= 90:
        return COLORS["90"]
    if value.number >= 80:
        return COLORS["80"]
    if value.number >= 70:
        return COLORS["70"]
    if value.number >= 60:
        return COLORS["60"]
    return COLORS["low"]


def percent_text(value: PercentValue | None) -> str:
    if value is None:
        return "／"
    return value.text


def column_width(label: str) -> int:
    key = compact_text(label)
    if key == "组别":
        return 100
    if key == "进度":
        return 125
    if key == "考核":
        return 115
    if key == "姓名":
        return 160
    if any(token in key for token in SUMMARY_LABELS):
        return 185
    if len(key) <= 4:
        return 145
    if len(key) <= 8:
        return 185
    return 220


def draw_merged_column(draw: Any, rows: list[dict[str, Any]], key: str, left: int, top: int, width: int, row_height: int, used_font: Any) -> None:
    start = 0
    while start < len(rows):
        value = rows[start][key]
        end = start + 1
        while end < len(rows) and rows[end][key] == value:
            end += 1
        box = (left, top + start * row_height, left + width, top + end * row_height)
        fill = COLORS["body"]
        if key == "category":
            fill = "#E9F2FB" if "文生" in compact_text(value) else ("#F0ECF8" if "图生" in compact_text(value) else "#F1F3F6")
        draw.rectangle(box, fill=fill, outline=COLORS["divider"], width=2)
        centered(draw, box, value, used_font)
        start = end


def render_png(model: dict[str, Any], output: Path) -> dict[str, Any]:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise CompositionError("缺少Pillow，无法生成合并成绩图") from exc
    rows = model["rows"]
    headers = ["组别", "进度", "考核", "姓名", *model["dimensions"], model["summary_label"], *model["supplement_labels"]]
    widths = [column_width(label) for label in headers]
    margin, title_height, header_height, row_height, legend_height = 20, 76, 78, 58, 70
    table_width = sum(widths)
    table_height = header_height + row_height * len(rows)
    width = table_width + margin * 2
    height = margin + title_height + table_height + 20 + legend_height + margin
    if width * height > MAX_PNG_PIXELS:
        raise CompositionError(f"合并成绩图尺寸过大（{width}×{height}）；请按组拆分")

    title_font = find_font(30, True)
    header_font = find_font(19, True)
    body_font = find_font(18, False)
    strong_font = find_font(19, True)
    legend_font = find_font(17, False)
    image = Image.new("RGB", (width, height), COLORS["background"])
    draw = ImageDraw.Draw(image)
    centered(draw, (margin, margin, width - margin, margin + 64), model["title"], title_font)
    table_top = margin + title_height
    x = margin
    for label, column_width_value in zip(headers, widths):
        box = (x, table_top, x + column_width_value, table_top + header_height)
        draw.rectangle(box, fill=COLORS["header"], outline=COLORS["grid"], width=2)
        centered(draw, box, wrap_header(label), header_font)
        x += column_width_value

    body_top = table_top + header_height
    fixed_width = sum(widths[:4])
    data_left = margin + fixed_width
    for row_index, row in enumerate(rows):
        top = body_top + row_index * row_height
        name_left = margin + sum(widths[:3])
        name_box = (name_left, top, name_left + widths[3], top + row_height)
        draw.rectangle(name_box, fill=COLORS["body"], outline=COLORS["grid"], width=2)
        centered(draw, name_box, row["student"], body_font)
        values = [*row["values"], row["overall"], *row["supplements"]]
        x = data_left
        for value_index, (value, column_width_value) in enumerate(zip(values, widths[4:])):
            box = (x, top, x + column_width_value, top + row_height)
            draw.rectangle(box, fill=score_fill(value), outline=COLORS["grid"], width=2)
            is_emphasis = value_index >= len(row["values"])
            centered(draw, box, percent_text(value), strong_font if is_emphasis else body_font, COLORS["muted"] if value is None else COLORS["text"])
            if value is None:
                draw.line((x + 14, top + row_height - 10, x + column_width_value - 14, top + 10), fill="#AAB3BF", width=2)
            x += column_width_value

    left = margin
    for key, width_value in zip(("group", "progress", "category"), widths[:3]):
        draw_merged_column(draw, rows, key, left, body_top, width_value, row_height, strong_font if key != "progress" else body_font)
        left += width_value
    for index in range(1, len(rows)):
        group_changed = rows[index]["group"] != rows[index - 1]["group"]
        progress_changed = rows[index]["progress"] != rows[index - 1]["progress"]
        category_changed = rows[index]["category"] != rows[index - 1]["category"]
        if group_changed or progress_changed or category_changed:
            y = body_top + index * row_height
            left = margin
            for changed, segment_width in zip(
                (group_changed, progress_changed, category_changed),
                widths[:3],
            ):
                if changed:
                    draw.line((left, y, left + segment_width, y), fill=COLORS["divider"], width=3)
                left += segment_width
            name_left = margin + sum(widths[:3])
            draw.line((name_left, y, margin + table_width, y), fill=COLORS["divider"], width=3)

    legend_top = table_top + table_height + 20
    draw.rounded_rectangle((margin, legend_top, margin + table_width, legend_top + legend_height), radius=12, fill="#F8FAFD", outline="#DCE2EA", width=2)
    draw.text((margin + 20, legend_top + 22), "准确率色阶", fill=COLORS["text"], font=strong_font)
    legend = [("90–100%", COLORS["90"]), ("80–89%", COLORS["80"]), ("70–79%", COLORS["70"]), ("60–69%", COLORS["60"]), ("<60%", COLORS["low"]), ("／ 不适用", COLORS["missing"])]
    start = margin + 170
    slot = (table_width - 190) / len(legend)
    for index, (label, color) in enumerate(legend):
        left = int(start + index * slot)
        draw.rounded_rectangle((left, legend_top + 17, left + 44, legend_top + 53), radius=5, fill=color)
        draw.text((left + 54, legend_top + 22), label, font=legend_font, fill=COLORS["muted"])

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".compose-", dir=output.parent) as temp_name:
        staged = Path(temp_name) / "result.png"
        image.save(staged, format="PNG", optimize=True)
        os.replace(staged, output)
    missing_cells = sum(value is None for row in rows for value in [*row["values"], row["overall"], *row["supplements"]])
    return {"width": width, "height": height, "row_count": len(rows), "missing_cells": missing_cells}


def safe_filename_part(value: str, *, fallback: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", str(value).strip())
    text = re.sub(r"\s+", "", text).strip(".-")
    return (text or fallback)[:120]


def output_name(model: dict[str, Any], *, split: bool) -> str:
    title = safe_filename_part(model["title"], fallback="成绩汇总")
    if not split:
        return f"{title}.png"
    groups = safe_filename_part("-".join(model["groups"]), fallback="未分组")
    return f"{title}-{groups}-{model['structure_signature'][:8]}.png"


def output_record(model: dict[str, Any], path: Path, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "groups": model["groups"],
        "structure_signature": model["structure_signature"],
        **details,
    }


def commit_staged_files(staged: list[tuple[Path, Path]], backup_root: Path) -> None:
    backup_root.mkdir(parents=True, exist_ok=True)
    committed: list[tuple[Path, Path | None]] = []
    try:
        for index, (staged_path, target) in enumerate(staged):
            if target.is_dir():
                raise CompositionError(f"输出文件与已有目录冲突：{target}")
            backup: Path | None = None
            if target.exists():
                backup = backup_root / f"{index:03d}-{target.name}"
                os.replace(target, backup)
            try:
                os.replace(staged_path, target)
            except OSError:
                if backup is not None and backup.exists():
                    os.replace(backup, target)
                raise
            committed.append((target, backup))
    except (CompositionError, OSError):
        for target, backup in reversed(committed):
            if target.exists() and target.is_file():
                target.unlink()
            if backup is not None and backup.exists():
                os.replace(backup, target)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = CompositionArgumentParser(description="从已提取的成绩图片数据生成动态维度合并色阶图")
    parser.add_argument("--composition-json", required=True, type=Path)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path)
    destination.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if not args.composition_json.is_absolute():
        raise CompositionError("--composition-json必须是绝对路径")
    if args.output is not None:
        if not args.output.is_absolute():
            raise CompositionError("--output必须是绝对路径")
        if args.output.suffix.lower() != ".png":
            raise CompositionError("--output必须以.png结尾")
    if args.output_dir is not None and not args.output_dir.is_absolute():
        raise CompositionError("--output-dir必须是绝对路径")
    return args


def stopped_payload(reason: str) -> dict[str, Any]:
    return {
        "status": "stopped",
        "png": None,
        "pngs": [],
        "stopped_items": [{
            "stage": "image_composition",
            "reason": reason,
            "next_action": "修正composition JSON中的图片提取结果、姓名、维度或合图指令后重试",
        }],
    }


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        payload = load_payload(args.composition_json)
        models = build_models(payload)
        if args.output is not None:
            if len(models) != 1:
                details = [f"{','.join(model['groups'])}:{model['structure_signature']}" for model in models]
                raise CompositionError(f"检测到多种考试结构，请使用--output-dir自动分图：{details}")
            model = models[0]
            details = render_png(model, args.output)
            record = output_record(model, args.output, details)
            print(json.dumps({
                "status": "complete",
                "png": str(args.output.resolve()),
                "pngs": [record],
                "dimensions": model["dimensions"],
                "supplemental_columns": model["supplement_labels"],
                **details,
            }, ensure_ascii=False))
            return 0

        output_dir = args.output_dir
        assert output_dir is not None
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        split = len(models) > 1
        records: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix=".compose-batch-", dir=output_dir.parent) as temp_name:
            stage_root = Path(temp_name)
            staged: list[tuple[Path, Path]] = []
            for model in models:
                name = output_name(model, split=split)
                staged_path = stage_root / name
                target = output_dir / name
                details = render_png(model, staged_path)
                staged.append((staged_path, target))
                records.append(output_record(model, target, details))
            output_dir.mkdir(parents=True, exist_ok=True)
            commit_staged_files(staged, stage_root / "backups")
        print(json.dumps({
            "status": "complete",
            "png": records[0]["path"] if len(records) == 1 else None,
            "pngs": records,
        }, ensure_ascii=False))
        return 0
    except (CompositionError, OSError) as exc:
        print(json.dumps(stopped_payload(str(exc)), ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
