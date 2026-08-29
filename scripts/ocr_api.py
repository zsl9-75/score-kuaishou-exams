#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import math
import mimetypes
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


class ApiOcrError(RuntimeError):
    pass


def _normalize_id(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return str(int(value)) if value.is_integer() else format(value, ".15g")
    text = re.sub(r"\s+", "", str(value))
    if re.fullmatch(r"[+-]?\d+\.0+", text):
        return str(int(float(text)))
    if re.fullmatch(r"\d+", text):
        return str(int(text))
    return text


def _required_setting(name: str, fallback: str = "") -> str:
    value = str(os.environ.get(name) or fallback).strip()
    if not value:
        raise ApiOcrError(f"缺少环境变量 {name}")
    return value


def _data_url(path: Path) -> str:
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{media_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _prompt(role: str, dimensions: list[str]) -> str:
    role_text = "标准答案" if role == "standard" else "学员作业"
    return (
        f"你在读取一张{role_text}评分表截图。目标评分维度是：{json.dumps(dimensions, ensure_ascii=False)}。"
        "请识别可见的姓名、题目ID列、目标维度表头和对应答案。不要按物理列号猜测，先识别表头，再按表头归属单元格。"
        "只输出一个JSON对象，不要Markdown。格式："
        '{"student_name":"学员姓名或空字符串","headers":["ID","实际识别到的维度表头"],'
        '"rows":[["题目ID","答案"]],"confidence":{"题目ID":0.95}}。'
        "headers与每行列数必须一致；只保留ID和目标维度，不要归因、解释、备注列；看不清的文字保留空字符串，不能编造。"
        + ("这是标准答案，student_name必须为空字符串。" if role == "standard" else "优先从姓名/学员姓名/同学名称字段识别姓名；没有则留空。")
    )


def _extract_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    for output in payload.get("output", []) if isinstance(payload.get("output"), list) else []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []) if isinstance(output.get("content"), list) else []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                return content["text"]
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content
    raise ApiOcrError("OCR API 响应中没有可解析的文本结果")


def _parse_json_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ApiOcrError("OCR API 没有返回有效 JSON") from exc
    if not isinstance(payload, dict):
        raise ApiOcrError("OCR API 结果必须是 JSON 对象")
    headers = payload.get("headers")
    rows = payload.get("rows")
    if not isinstance(headers, list) or len(headers) < 2:
        raise ApiOcrError("OCR API 结果缺少 headers")
    if not isinstance(rows, list) or not rows:
        raise ApiOcrError("OCR API 结果没有数据行")
    normalized_rows: list[list[Any]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, list) or len(row) != len(headers):
            raise ApiOcrError(f"OCR API 第{index}行与表头列数不一致")
        normalized_rows.append(row)
    confidence = payload.get("confidence")
    if not isinstance(confidence, dict):
        raise ApiOcrError("OCR API confidence 必须是逐题数值对象，不能缺失")
    row_ids = [_normalize_id(row[0]) for row in normalized_rows]
    if any(not row_id for row_id in row_ids) or len(row_ids) != len(set(row_ids)):
        raise ApiOcrError("OCR API 结果的题目ID为空或规范化后重复")
    normalized_confidence: dict[str, float] = {}
    for raw_key, raw_value in confidence.items():
        row_id = _normalize_id(raw_key)
        if not row_id or row_id in normalized_confidence:
            raise ApiOcrError("OCR API confidence 的ID为空或规范化后重复")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ApiOcrError(f"OCR API confidence[{raw_key!r}] 必须是0–1有限数值")
        value = float(raw_value)
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ApiOcrError(f"OCR API confidence[{raw_key!r}] 必须在0–1之间")
        normalized_confidence[row_id] = value
    missing_confidence = [row_id for row_id in row_ids if row_id not in normalized_confidence]
    extra_confidence = [row_id for row_id in normalized_confidence if row_id not in set(row_ids)]
    if missing_confidence or extra_confidence:
        raise ApiOcrError(f"OCR API confidence 与题目ID未完全对齐：缺失={missing_confidence}，额外={extra_confidence}")
    payload["student_name"] = str(payload.get("student_name") or "").strip()
    payload["headers"] = headers
    payload["rows"] = normalized_rows
    payload["confidence"] = normalized_confidence
    return payload


def recognize_one(path: Path, *, role: str, dimensions: list[str], timeout: float = 120) -> dict[str, Any]:
    api_key = _required_setting("SCORE_OCR_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    model = _required_setting("SCORE_OCR_API_MODEL")
    api_url = str(os.environ.get("SCORE_OCR_API_URL") or "https://api.openai.com/v1/responses").strip()
    request_payload = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _prompt(role, dimensions)},
                    {"type": "input_image", "image_url": _data_url(path), "detail": "high"},
                ],
            }
        ],
    }
    if api_url.rstrip("/").endswith("chat/completions"):
        request_payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _prompt(role, dimensions)},
                        {"type": "image_url", "image_url": {"url": _data_url(path), "detail": "high"}},
                    ],
                }
            ],
            "response_format": {"type": "json_object"},
        }
    started = time.perf_counter()
    request = urllib.request.Request(
        api_url,
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise ApiOcrError(f"OCR API HTTP {exc.code}：{detail}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ApiOcrError(f"OCR API 调用失败：{exc}") from exc
    parsed = _parse_json_text(_extract_text(response_payload))
    parsed.update({"path": str(path.resolve()), "engine": "api", "elapsed_seconds": round(time.perf_counter() - started, 3)})
    return parsed


def recognize_many(paths: list[Path], *, role: str, dimensions: list[str], workers: int = 4) -> list[dict[str, Any]]:
    if not paths:
        return []
    started = time.perf_counter()
    results: dict[Path, dict[str, Any]] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 8))) as executor:
        futures = {executor.submit(recognize_one, path, role=role, dimensions=dimensions): path for path in paths}
        for future in as_completed(futures):
            path = futures[future]
            try:
                results[path] = future.result()
            except Exception as exc:
                errors.append(f"{path.name}：{exc}")
    if errors:
        raise ApiOcrError("；".join(errors))
    ordered = [results[path] for path in paths]
    total = round(time.perf_counter() - started, 3)
    for item in ordered:
        item["batch_elapsed_seconds"] = total
        item["workers"] = max(1, min(int(workers), 8))
    return ordered
