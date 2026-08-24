#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

import manifest_runtime as runtime


MODULE_PATH = Path(__file__).with_name("run_assessment.py")
SPEC = importlib.util.spec_from_file_location("score_kuaishou_pipeline", MODULE_PATH)
assert SPEC and SPEC.loader
pipeline = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pipeline
SPEC.loader.exec_module(pipeline)


def document(headers, rows, *, student_name="", source="docs", document_id="test", revision="r1"):
    payload = {
        "schema_version": 1,
        "source": source,
        "document": {"url": f"https://docs.corp.kuaishou.com/{document_id}", "id": document_id, "revision": revision},
        "sheet": "Sheet1",
        "range": "A1:Z100",
        "headers": headers,
        "rows": rows,
        "_source_path": "fixture.json",
    }
    if student_name:
        payload["student_name"] = student_name
    return payload


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def manifest_payload(*, standard="standard.json", homework=None, max_concurrency=None):
    runtime_config = {}
    if max_concurrency is not None:
        runtime_config["max_concurrency"] = max_concurrency
    return {
        "schema_version": 1,
        "task_id": "task-29-quiz",
        "exam_profile": "retake-image-aesthetic",
        "group": "29组",
        "progress": "补考画面美学",
        "standard": {
            "id": "standard-doc",
            "url": "https://docs.corp.kuaishou.com/standard-doc",
            "revision": "std-r1",
            "sheet": "Sheet1",
            "range": "A1:Z100",
            "evidence": standard,
        },
        "homework": {
            "documents": homework
            or [
                {
                    "student": "张三",
                    "id": "student-zhang",
                    "url": "https://docs.corp.kuaishou.com/student-zhang",
                    "revision": "zhang-r1",
                    "sheet": "Sheet1",
                    "range": "A1:Z100",
                    "evidence": "zhang.json",
                },
                {
                    "student": "李四",
                    "id": "student-li",
                    "url": "https://docs.corp.kuaishou.com/student-li",
                    "revision": "li-r1",
                    "sheet": "Sheet1",
                    "range": "A1:Z100",
                    "evidence": "li.json",
                },
            ]
        },
        "runtime": runtime_config,
        "output": {"dir": "output", "png": "off", "xlsx": "off"},
    }


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = pipeline.load_config()

    def test_profiles_are_fixed_and_complete(self):
        profiles = self.config["profiles"]
        self.assertEqual(len(profiles), 10)
        self.assertEqual(len(profiles["quiz-text-to-video"]["dimensions"]), 6)
        self.assertEqual(len(profiles["final-text-to-video"]["dimensions"]), 10)
        self.assertEqual(len(profiles["final-image-to-video"]["dimensions"]), 11)
        self.assertEqual(profiles["retake-text-to-video"]["dimensions"], profiles["final-text-to-video"]["dimensions"])
        self.assertEqual(profiles["retake-image-to-video"]["dimensions"], profiles["final-image-to-video"]["dimensions"])

    def test_id_normalization(self):
        self.assertEqual(pipeline.normalize_id(" 0046 "), "46")
        self.assertEqual(pipeline.normalize_id("46.0"), "46")
        self.assertEqual(pipeline.normalize_id(46.0), "46")
        self.assertEqual(pipeline.normalize_id("A-046"), "A-046")

    def test_keyword_matching_uses_bidirectional_containment(self):
        cases = [
            (("videoContrast",), "videoContrast好", ("videoContrast",)),
            (("videoExp",), "videoExp好", ("videoExp",)),
            (("都一般", "一样好"), "答案：一样好", ("一样好",)),
            (("都一般", "videoExp好"), "作业选择 videoExp好", ("videoExp好",)),
            (("video Contrast",), "videoContrast好", ("video Contrast",)),
            (("videoContrast好",), "videoContrast", ("videoContrast好",)),
            (("都一般",), "般", ("都一般",)),
        ]
        for keywords, homework, expected in cases:
            with self.subTest(keywords=keywords, homework=homework):
                self.assertEqual(pipeline.match_standard_keywords(homework, keywords), expected)
        self.assertEqual(pipeline.match_standard_keywords("一样差", ("都一般", "一样好")), ())

    def test_standard_multi_answer_text_delimiters(self):
        for separator in ["\n", ",", "，", "、", "/", "／", "|", "｜"]:
            with self.subTest(separator=repr(separator)):
                keywords = pipeline.standard_answer_keywords(f"都一般{separator}一样好")
                self.assertEqual(keywords, ("都一般", "一样好"))
        self.assertEqual(
            pipeline.standard_answer_keywords("都一般，，"),
            ("都一般",),
        )

    def test_standard_multi_answer_structured_values_and_deduplication(self):
        cases = [
            ["videoContrast", "自定义关键词"],
            [{"text": "videoContrast"}, {"value": "自定义关键词"}],
            {"values": [{"label": "videoContrast"}, {"name": "自定义关键词"}]},
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(pipeline.standard_answer_keywords(raw), ("videoContrast", "自定义关键词"))
        self.assertEqual(pipeline.standard_answer_keywords(["任意文本", "任意文本", "另一个答案"]), ("任意文本", "另一个答案"))
        self.assertEqual(pipeline.standard_answer_keywords({"value": "internal-option-id", "text": "用户可见关键词"}), ("用户可见关键词",))

    def test_standard_keywords_accept_arbitrary_text_but_reject_mixed_blank(self):
        self.assertEqual(pipeline.standard_answer_keywords("不在旧五值中的原始关键词"), ("不在旧五值中的原始关键词",))
        with self.assertRaisesRegex(pipeline.AssessmentError, "空值与非空关键词混合"):
            pipeline.standard_answer_keywords([None, "一样好"])
        with self.assertRaisesRegex(pipeline.AssessmentError, "结构无法识别"):
            pipeline.standard_answer_keywords({"color": "blue"})

    def test_multi_answer_matches_any_option_and_counts_once(self):
        dimensions = ["画面美学"]
        raw_options = {"values": [{"text": "videoContrast"}, {"text": "自定义关键词"}, {"text": "自定义关键词"}]}
        standard_docs = [document(["ID", "画面美学"], [[1, raw_options], [2, "另一个固定答案"]])]
        homework_docs = [document(["同学名称", "ID", "画面美学"], [["张三", 1, "videoContrast好"], ["张三", 2, "不相关内容"]])]
        standard = pipeline.parse_standard(standard_docs, dimensions, self.config)
        self.assertEqual(standard["画面美学"]["1"]["keywords"], ("videoContrast", "自定义关键词"))
        self.assertEqual(standard["画面美学"]["1"]["raw_value"], raw_options)
        students, anomalies = pipeline.parse_homework_documents(homework_docs, dimensions, self.config, {})
        result = pipeline.score_students(standard, students, dimensions, anomalies)
        dimension = result["summary"][0]["dimensions"]["画面美学"]
        self.assertEqual((dimension["correct"], dimension["total"], dimension["accuracy"]), (1, 2, 0.5))
        first, second = result["details"]
        self.assertEqual(first["standard_keywords"], "videoContrast｜自定义关键词")
        self.assertEqual(first["matched_keywords"], "videoContrast")
        self.assertIn("双向模糊命中标准关键词", first["note"])
        self.assertIn("未与任何标准关键词形成包含关系", second["note"])

    def test_all_profiles_only_read_declared_dimensions(self):
        for profile_key, profile in self.config["profiles"].items():
            dimensions = profile["dimensions"]
            headers = ["ID", *dimensions, "解释", "任意额外维度"]
            row = [46, *["都一般" for _ in dimensions], "不参与", "一样差"]
            parsed = pipeline.parse_standard([document(headers, [row])], dimensions, self.config)
            self.assertEqual(list(parsed), dimensions, profile_key)
            self.assertTrue(all(list(values) == ["46"] for values in parsed.values()), profile_key)

    def test_non_special_standard_blank_is_fatal(self):
        with self.assertRaisesRegex(pipeline.AssessmentError, "不允许标准空值"):
            pipeline.parse_standard([document(["ID", "画面美学"], [[46, ""]])], ["画面美学"], self.config)

    def test_special_blank_matrix(self):
        dimensions = ["多镜头指令遵循", "多镜头间一致连贯性"]
        standard_docs = [document(["ID", *dimensions], [[1, "", ""], [2, "videoExp", "videoContrast"]])]
        homework_docs = [
            document(
                ["同学名称", "ID", *dimensions],
                [
                    ["张三", 2, "videoExp好", ""],
                    ["张三", 1, "", "C好"],
                ],
            )
        ]
        standard = pipeline.parse_standard(standard_docs, dimensions, self.config)
        students, anomalies = pipeline.parse_homework_documents(homework_docs, dimensions, self.config, {})
        result = pipeline.score_students(standard, students, dimensions, anomalies)
        first, second = [result["summary"][0]["dimensions"][dim] for dim in dimensions]
        self.assertEqual((first["correct"], first["total"]), (2, 2))
        self.assertEqual((second["correct"], second["total"]), (0, 2))
        self.assertEqual(second["wrong_ids"], ["1", "2"])

    def test_shuffled_ids_missing_extra_and_equal_weight(self):
        dimensions = ["画面美学", "动态美学"]
        standard_docs = [document(["ID", *dimensions], [[1, "都一般", "一样好"], [2, "videoExp", "videoContrast"]])]
        homework_docs = [
            document(
                ["同学名称", "动态美学", "ID", "画面美学", "解释"],
                [
                    ["李四", "videoContrast好", 2, "videoExp好", "忽略"],
                    ["李四", "一样差", 3, "般", "额外ID"],
                    ["李四", "一样好", 1, "错误文本", "待复核"],
                ],
            )
        ]
        standard = pipeline.parse_standard(standard_docs, dimensions, self.config)
        students, anomalies = pipeline.parse_homework_documents(homework_docs, dimensions, self.config, {})
        result = pipeline.score_students(standard, students, dimensions, anomalies)
        summary = result["summary"][0]
        self.assertEqual(summary["dimensions"]["画面美学"]["accuracy"], 0.5)
        self.assertEqual(summary["dimensions"]["动态美学"]["accuracy"], 1.0)
        self.assertEqual(summary["overall_accuracy"], 0.75)
        self.assertTrue(any(item["type"] == "额外ID" and item["id"] == "3" for item in result["anomalies"]))

    def test_duplicate_id_is_fatal(self):
        with self.assertRaisesRegex(pipeline.AssessmentError, "重复 ID"):
            pipeline.parse_standard(
                [document(["ID", "画面美学"], [[46, "都一般"], [46, "一样好"]])],
                ["画面美学"],
                self.config,
            )

    def test_evidence_schema_version_is_required(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "bad.json"
            payload = document(["ID", "画面美学"], [[46, "都一般"]])
            payload["schema_version"] = 2
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(pipeline.AssessmentError, "schema_version"):
                pipeline.load_evidence_files([path])

    def test_synthetic_ocr_handles_merged_and_split_cells(self):
        def hit(text, x, y, width=0.05, confidence=1.0):
            return {"text": text, "x": x, "y": y, "width": width, "height": 0.03, "confidence": confidence}

        hits = [
            hit("ID", 0.02, 0.94, 0.02),
            hit("动态美学", 0.10, 0.94, 0.07),
            hit("videoContrast动态美学归因", 0.28, 0.94, 0.20),
            hit("46 videoContrast好", 0.08, 0.84, 0.14, 0.5),
            hit("54", 0.08, 0.74, 0.02),
            hit("都一般", 0.11, 0.74, 0.05),
            hit("3", 0.003, 0.74, 0.01),
        ]
        rows, confidence = pipeline.parse_ocr_rows(hits, "动态美学")
        self.assertEqual(rows, [["46", "videoContrast好"], ["54", "都一般"]])
        self.assertEqual(confidence["46"], 0.5)

    def test_manifest_defaults_to_15_concurrency_and_rejects_more(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            path = temp / "task.json"
            write_json(path, manifest_payload())
            loaded = runtime.load_manifest(path)
            self.assertEqual(loaded["runtime"]["max_concurrency"], 15)
            payload = manifest_payload(max_concurrency=16)
            write_json(path, payload)
            with self.assertRaisesRegex(runtime.ManifestError, "1–15"):
                runtime.load_manifest(path)

    def test_specs_command_lists_stable_item_ids_without_writing_state(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            manifest_path = temp / "task.json"
            write_json(manifest_path, manifest_payload())
            loaded = runtime.load_manifest(manifest_path)
            self.assertFalse(runtime.state_path(loaded).exists())
            completed = subprocess.run(
                [sys.executable, str(Path(runtime.__file__)), "specs", "--manifest", str(manifest_path), "--stage", "students"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual([item["student"] for item in payload["items"]], ["张三", "李四"])
            self.assertTrue(all(item["item_id"] for item in payload["items"]))
            self.assertFalse(runtime.state_path(loaded).exists())

    def test_revision_cache_and_failed_document_resume(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            path = temp / "task.json"
            write_json(path, manifest_payload())
            manifest = runtime.load_manifest(path)

            standard_spec = runtime.document_specs(manifest, runtime.load_state(manifest), stage="initial")
            initial = runtime.plan_reads(manifest, standard_spec)
            self.assertEqual(initial["max_concurrency"], 15)
            self.assertEqual(len(initial["read"]), 1)
            standard_file = temp / "standard-evidence.json"
            write_json(standard_file, document(["ID", "画面美学"], [[1, "都一般"]], document_id="standard-doc", revision="std-r1"))
            standard_item = initial["read"][0]["item_id"]
            runtime.ingest_evidence(manifest, standard_item, standard_file)
            repeated = runtime.plan_reads(manifest, runtime.document_specs(manifest, runtime.load_state(manifest), stage="initial"))
            self.assertEqual(len(repeated["cached"]), 1)
            self.assertEqual(repeated["read"], [])

            student_specs = runtime.document_specs(manifest, runtime.load_state(manifest), stage="students")
            students_plan = runtime.plan_reads(manifest, student_specs)
            by_student = {item["student"]: item for item in students_plan["read"]}
            zhang_file = temp / "zhang-evidence.json"
            write_json(zhang_file, document(["ID", "画面美学"], [[1, "都一般"]], student_name="张三", document_id="student-zhang", revision="zhang-r1"))
            runtime.ingest_evidence(manifest, by_student["张三"]["item_id"], zhang_file)
            runtime.record_failure(manifest, by_student["李四"]["item_id"], "Docs timeout", "timeout")
            retry_state = runtime.load_state(manifest)
            retry_state["documents"][by_student["李四"]["item_id"]]["next_attempt_at"] = "2000-01-01T00:00:00+00:00"
            runtime.save_state(manifest, retry_state)
            resume = runtime.plan_reads(manifest, runtime.document_specs(manifest, runtime.load_state(manifest), stage="students"))
            self.assertEqual([item["student"] for item in resume["cached"]], ["张三"])
            self.assertEqual([item["student"] for item in resume["read"]], ["李四"])

    def test_first_homework_learns_range_and_reuses_it_for_same_task(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            homework = [
                {"student": "张三", "id": "student-zhang", "url": "https://docs.corp.kuaishou.com/student-zhang", "revision": "zhang-r1"},
                {"student": "李四", "id": "student-li", "url": "https://docs.corp.kuaishou.com/student-li", "revision": "li-r1"},
            ]
            payload = manifest_payload(homework=homework)
            payload["homework"]["layout_reuse"] = {"enabled": True, "discovery_range": "A1:AB200"}
            manifest_path = temp / "task.json"
            write_json(manifest_path, payload)
            manifest = runtime.load_manifest(manifest_path)

            student_specs = runtime.document_specs(manifest, runtime.load_state(manifest), stage="students")
            preflight = temp / "preflight.json"
            write_json(preflight, {"items": [
                {"item_id": item["item_id"], "revision": item["revision"], "sheet": "作业", "range": "A1:AB200"}
                for item in student_specs
            ]})
            first_plan = runtime.plan_reads(manifest, runtime.merge_revisions(student_specs, preflight))
            self.assertEqual(len(first_plan["read"]), 1)
            self.assertEqual(first_plan["read"][0]["read_mode"], "discovery_probe")
            self.assertEqual(first_plan["read"][0]["discovery_range"], "A1:AB200")
            self.assertEqual(first_plan["read"][0]["range"], "")
            self.assertEqual([item["student"] for item in first_plan["deferred"]], ["李四"])
            self.assertEqual({item["student"] for item in pipeline.manifest_failures(manifest)}, {"张三", "李四"})

            probe = first_plan["read"][0]
            probe_payload = document(["order", "画面美学"], [[1, "都一般"], [2, "一样好"]], student_name="张三", document_id="student-zhang", revision="zhang-r1")
            probe_payload["sheet"] = "作业"
            probe_payload["range"] = "A1:AB3"
            probe_file = temp / "probe.json"
            write_json(probe_file, probe_payload)
            ingested = runtime.ingest_evidence(manifest, probe["item_id"], probe_file)
            self.assertEqual(ingested["learned_layout"]["range"], "A1:AB3")
            self.assertEqual(ingested["learned_layout"]["id_count"], 2)

            second_plan = runtime.plan_reads(manifest, runtime.document_specs(manifest, runtime.load_state(manifest), stage="students"))
            self.assertEqual([item["student"] for item in second_plan["cached"]], ["张三"])
            self.assertEqual([item["student"] for item in second_plan["read"]], ["李四"])
            self.assertEqual(second_plan["read"][0]["read_mode"], "learned_fast")
            self.assertEqual(second_plan["read"][0]["sheet"], "作业")
            self.assertEqual(second_plan["read"][0]["range"], "A1:AB3")

    def test_learned_range_mismatch_falls_back_only_for_that_student(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            homework = [
                {"student": "张三", "id": "student-zhang", "url": "https://docs.corp.kuaishou.com/student-zhang", "revision": "zhang-r1"},
                {"student": "李四", "id": "student-li", "url": "https://docs.corp.kuaishou.com/student-li", "revision": "li-r1"},
            ]
            payload = manifest_payload(homework=homework)
            manifest_path = temp / "task.json"
            write_json(manifest_path, payload)
            manifest = runtime.load_manifest(manifest_path)
            first = runtime.plan_reads(manifest, runtime.document_specs(manifest, runtime.load_state(manifest), stage="students"))
            probe = first["read"][0]
            probe_payload = document(["order", "画面美学"], [[1, "都一般"], [2, "一样好"]], student_name="张三", document_id="student-zhang", revision="zhang-r1")
            probe_payload.update({"sheet": "作业", "range": "A1:AB3"})
            probe_file = temp / "probe.json"
            write_json(probe_file, probe_payload)
            runtime.ingest_evidence(manifest, probe["item_id"], probe_file)

            second = runtime.plan_reads(manifest, runtime.document_specs(manifest, runtime.load_state(manifest), stage="students"))
            li = second["read"][0]
            mismatch_payload = document(["order", "画面美学"], [[1, "都一般"]], student_name="李四", document_id="student-li", revision="li-r1")
            mismatch_payload.update({"sheet": "例外作业", "range": "B2:AC3"})
            mismatch_file = temp / "mismatch.json"
            write_json(mismatch_file, mismatch_payload)
            result = runtime.ingest_evidence(manifest, li["item_id"], mismatch_file)
            self.assertEqual(result["status"], "needs_discovery")

            fallback = runtime.plan_reads(manifest, runtime.document_specs(manifest, runtime.load_state(manifest), stage="students"))
            fallback_li = next(item for item in fallback["read"] if item["student"] == "李四")
            self.assertEqual(fallback_li["read_mode"], "discovery_fallback")
            self.assertEqual(fallback_li["range"], "")
            fallback_payload = document(["order", "画面美学"], [[1, "都一般"], [2, "一样好"], [3, "一样差"]], student_name="李四", document_id="student-li", revision="li-r1")
            fallback_payload.update({"sheet": "例外作业", "range": "B2:AC5"})
            fallback_file = temp / "fallback.json"
            write_json(fallback_file, fallback_payload)
            completed = runtime.ingest_evidence(manifest, fallback_li["item_id"], fallback_file)
            self.assertEqual(completed["status"], "success")
            self.assertEqual(runtime.load_state(manifest)["layouts"]["homework"]["range"], "A1:AB3")
            final_plan = runtime.plan_reads(manifest, runtime.document_specs(manifest, runtime.load_state(manifest), stage="students"))
            cached_li = next(item for item in final_plan["cached"] if item["student"] == "李四")
            self.assertEqual(cached_li["read_mode"], "fixed_exception")
            self.assertEqual(cached_li["range"], "B2:AC5")

    def test_batch_ingest_and_retry_classification(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            manifest_path = temp / "task.json"
            write_json(manifest_path, manifest_payload())
            manifest = runtime.load_manifest(manifest_path)
            plan = runtime.plan_reads(manifest, runtime.document_specs(manifest, runtime.load_state(manifest), stage="students"))
            evidence_dir = temp / "incoming"
            evidence_dir.mkdir()
            for item in plan["read"]:
                payload = document(["ID", "画面美学"], [[1, "都一般"]], student_name=item["student"], document_id=item["document_id"], revision=item["revision"])
                write_json(evidence_dir / f"{item['item_id']}.json", payload)
            batch = runtime.ingest_evidence_batch(manifest, evidence_dir)
            self.assertEqual(batch["ingested"], 2)
            self.assertEqual(batch["failed"], 0)

            refreshed = runtime.plan_reads(manifest, runtime.document_specs(manifest, runtime.load_state(manifest), stage="students"), refresh=True)
            target = refreshed["read"][0]
            failure = runtime.record_failure(manifest, target["item_id"], "Docs timeout", "timeout")
            self.assertEqual(failure["status"], "pending_retry")
            waiting = runtime.plan_reads(manifest, runtime.document_specs(manifest, runtime.load_state(manifest), stage="students"), refresh=True)
            self.assertTrue(any(item["item_id"] == target["item_id"] for item in waiting["retry"]))

            fatal = runtime.record_failure(manifest, target["item_id"], "permission denied", "permission")
            self.assertEqual(fatal["status"], "failed")
            layout_failure = runtime.record_failure(manifest, target["item_id"], "已学习范围不再有效", "layout_mismatch")
            self.assertEqual(layout_failure["status"], "needs_discovery")
            fallback = runtime.plan_reads(manifest, runtime.document_specs(manifest, runtime.load_state(manifest), stage="students"))
            fallback_target = next(item for item in fallback["read"] if item["item_id"] == target["item_id"])
            self.assertEqual(fallback_target["read_mode"], "discovery_fallback")

    def test_preflight_reports_all_fatal_and_retryable_errors(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            manifest_path = temp / "task.json"
            write_json(manifest_path, manifest_payload())
            manifest = runtime.load_manifest(manifest_path)
            specs = runtime.document_specs(manifest, runtime.load_state(manifest), stage="students")
            snapshot = temp / "preflight.json"
            write_json(snapshot, {"items": [
                {"item_id": specs[0]["item_id"], "error_kind": "permission", "error": "无权访问"},
                {"item_id": specs[1]["item_id"], "error_kind": "timeout", "error": "Docs timeout"},
            ]})
            planned = runtime.plan_reads(manifest, runtime.merge_revisions(specs, snapshot))
            self.assertEqual([item["student"] for item in planned["failed"]], ["张三"])
            self.assertEqual([item["student"] for item in planned["retry"]], ["李四"])
            self.assertEqual(planned["read"], [])

            write_json(snapshot, {"items": [
                {"item_id": specs[0]["item_id"], "error_kind": "permission", "error": "无权访问"},
                {"item_id": specs[1]["item_id"], "revision": "li-r1"},
            ]})
            recovered = runtime.plan_reads(manifest, runtime.merge_revisions(specs, snapshot))
            self.assertEqual([item["student"] for item in recovered["read"]], ["李四"])
            self.assertEqual(recovered["read"][0]["read_mode"], "fixed")

    def test_index_resolves_links_and_removes_deleted_students(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            payload = manifest_payload()
            payload["homework"] = {
                "index": {
                    "id": "index-doc",
                    "url": "https://docs.corp.kuaishou.com/index-doc",
                    "revision": "index-r1",
                    "sheet": "索引",
                    "range": "A1:B100",
                    "name_header": "同学名称",
                    "link_header": "作业链接",
                }
            }
            manifest_path = temp / "task.json"
            write_json(manifest_path, payload)
            manifest = runtime.load_manifest(manifest_path)
            initial = runtime.plan_reads(manifest, runtime.document_specs(manifest, runtime.load_state(manifest), stage="initial"))
            index_item = next(item for item in initial["read"] if item["role"] == "homework_index")
            index_file = temp / "index.json"
            index_payload = document(
                ["同学名称", "作业链接"],
                [["张三", "https://docs.corp.kuaishou.com/zhang"], ["李四", "https://docs.corp.kuaishou.com/li"]],
                document_id="index-doc",
                revision="index-r1",
            )
            index_payload["sheet"] = "索引"
            index_payload["range"] = "A1:B100"
            write_json(index_file, index_payload)
            runtime.ingest_evidence(manifest, index_item["item_id"], index_file)
            students = runtime.plan_reads(manifest, runtime.document_specs(manifest, runtime.load_state(manifest), stage="students"))
            self.assertEqual(students["max_concurrency"], 15)
            self.assertEqual({item["student"] for item in students["read"] + students["deferred"]}, {"张三", "李四"})

            li_spec = next(item for item in students["read"] + students["deferred"] if item["student"] == "李四")
            runtime.record_failure(manifest, li_spec["item_id"], "模拟失败")
            index_payload["rows"] = [["张三", "https://docs.corp.kuaishou.com/zhang"]]
            write_json(index_file, index_payload)
            runtime.ingest_evidence(manifest, index_item["item_id"], index_file)
            state = runtime.load_state(manifest)
            self.assertEqual(state["documents"][li_spec["item_id"]]["status"], "removed")
            self.assertFalse(any(item.get("student") == "李四" for item in pipeline.manifest_failures(manifest)))

    def test_manifest_incremental_scoring_and_json_default(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            manifest_path = temp / "task.json"
            write_json(manifest_path, manifest_payload())
            write_json(temp / "standard.json", document(["ID", "画面美学"], [[1, "都一般"], [2, "videoExp"]], document_id="standard-doc", revision="std-r1"))
            write_json(temp / "zhang.json", document(["ID", "画面美学"], [[1, "都一般"], [2, "videoExp好"]], student_name="张三", document_id="student-zhang", revision="zhang-r1"))
            write_json(temp / "li.json", document(["ID", "画面美学"], [[1, "一样差"], [2, "videoExp好"]], student_name="李四", document_id="student-li", revision="li-r1"))
            args = pipeline.parse_args(["--manifest", str(manifest_path)])

            xlsx, png, first = pipeline.run(args)
            self.assertIsNone(xlsx)
            self.assertIsNone(png)
            self.assertEqual(first["cache_stats"]["scores"], {"hits": 0, "misses": 2})
            self.assertTrue(Path(first["_result_path"]).exists())
            _, _, second = pipeline.run(args)
            self.assertEqual(second["cache_stats"]["scores"], {"hits": 2, "misses": 0})

            write_json(temp / "li.json", document(["ID", "画面美学"], [[1, "都一般"], [2, "videoExp好"]], student_name="李四", document_id="student-li", revision="li-r1"))
            _, _, changed_one = pipeline.run(args)
            self.assertEqual(changed_one["cache_stats"]["scores"], {"hits": 1, "misses": 1})

            write_json(temp / "standard.json", document(["ID", "画面美学"], [[1, "都一般"], [2, "videoContrast"]], document_id="standard-doc", revision="std-r1"))
            _, _, changed_standard = pipeline.run(args)
            self.assertEqual(changed_standard["cache_stats"]["scores"], {"hits": 0, "misses": 2})

    def test_incomplete_manifest_pauses_outputs_and_resumes_only_missing_student(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            payload = manifest_payload()
            payload["output"] = {"dir": "output", "png": "on", "xlsx": "on"}
            manifest_path = temp / "task.json"
            write_json(manifest_path, payload)
            write_json(temp / "standard.json", document(["ID", "画面美学"], [[1, "都一般"]], document_id="standard-doc", revision="std-r1"))
            write_json(temp / "zhang.json", document(["ID", "画面美学"], [[1, "都一般"]], student_name="张三", document_id="student-zhang", revision="zhang-r1"))
            args = pipeline.parse_args(["--manifest", str(manifest_path)])
            xlsx, png, incomplete = pipeline.run(args)
            self.assertIsNone(xlsx)
            self.assertIsNone(png)
            self.assertEqual(incomplete["run_status"], "incomplete")
            self.assertEqual([item["student"] for item in incomplete["failed_documents"]], ["李四"])
            self.assertTrue(Path(incomplete["_result_path"]).exists())

            write_json(temp / "li.json", document(["ID", "画面美学"], [[1, "都一般"]], student_name="李四", document_id="student-li", revision="li-r1"))
            xlsx, png, complete = pipeline.run(args)
            self.assertEqual(complete["run_status"], "complete")
            self.assertTrue(xlsx and xlsx.exists())
            self.assertTrue(png and png.exists())
            self.assertEqual(complete["cache_stats"]["scores"], {"hits": 1, "misses": 1})

    def test_summary_only_saves_json_and_result_json_renders_outputs(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            standard = temp / "standard.json"
            homework = temp / "homework.json"
            output = temp / "output"
            write_json(standard, document(["ID", "画面美学"], [[1, "都一般"]]))
            write_json(homework, document(["同学名称", "ID", "画面美学"], [["王五", 1, "都一般"]]))
            args = pipeline.parse_args([
                "--exam-profile", "retake-image-aesthetic", "--group", "29组", "--progress", "补考画面美学",
                "--standard-evidence", str(standard), "--homework-evidence", str(homework), "--output", str(output),
                "--summary-only", "--png", "on", "--xlsx", "on",
            ])
            xlsx, png, result = pipeline.run(args)
            self.assertIsNone(xlsx)
            self.assertIsNone(png)
            result_path = Path(result["_result_path"])
            self.assertTrue(result_path.exists())

            render_dir = temp / "rendered"
            render_args = pipeline.parse_args([
                "--result-json", str(result_path), "--output", str(render_dir), "--png", "on", "--xlsx", "on",
            ])
            xlsx, png, rendered = pipeline.run(render_args)
            self.assertTrue(xlsx and xlsx.exists())
            self.assertTrue(png and png.exists())
            self.assertEqual(rendered["summary"], result["summary"])

    def test_end_to_end_png_without_workbook(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            standard = temp / "standard.json"
            homework = temp / "homework.json"
            output = temp / "output"
            standard.write_text(json.dumps(document(["ID", "画面美学"], [[1, "般"], [2, "一样好"]]), ensure_ascii=False), encoding="utf-8")
            homework.write_text(json.dumps(document(["同学名称", "ID", "画面美学"], [["王五", 2, "一样差"], ["王五", 1, "般"]]), ensure_ascii=False), encoding="utf-8")
            args = pipeline.parse_args([
                "--exam-profile", "retake-image-aesthetic",
                "--group", "29组",
                "--progress", "补考画面美学",
                "--standard-evidence", str(standard),
                "--homework-evidence", str(homework),
                "--output", str(output),
                "--skip-xlsx",
                "--png", "on",
            ])
            _, png, result = pipeline.run(args)
            self.assertTrue(png.exists())
            self.assertGreater(png.stat().st_size, 1000)
            self.assertEqual(result["summary"][0]["dimensions"]["画面美学"]["accuracy"], 0.5)

    def test_end_to_end_xlsx_and_png(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            standard = temp / "standard.json"
            homework = temp / "homework.json"
            output = temp / "output"
            standard.write_text(
                json.dumps(document(["ID", "画面美学", "动态美学"], [[1, ["都一般", "一样好"], "一样好"], [2, "videoExp", "videoContrast好"]]), ensure_ascii=False),
                encoding="utf-8",
            )
            homework.write_text(
                json.dumps(
                    document(
                        ["同学名称", "动态美学", "ID", "画面美学", "解释"],
                        [
                            ["王五", "videoContrast好", 2, "videoExp好", "忽略"],
                            ["王五", "一样好", 1, "答案：都一般", "忽略"],
                            ["赵六", "一样差", 1, "一样好", "忽略"],
                            ["赵六", "videoContrast", 2, "videoExp好", "忽略"],
                        ],
                    ),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            args = pipeline.parse_args([
                "--exam-profile", "online-aesthetics-dual",
                "--group", "29组",
                "--progress", "画面美学＋动态美学",
                "--standard-evidence", str(standard),
                "--homework-evidence", str(homework),
                "--output", str(output),
                "--png", "on",
                "--xlsx", "on",
            ])
            xlsx, png, result = pipeline.run(args)
            self.assertIsNotNone(xlsx)
            self.assertTrue(xlsx.exists())
            self.assertGreater(xlsx.stat().st_size, 5000)
            self.assertTrue(png.exists())
            self.assertEqual(len(result["summary"]), 2)
            multi_details = [item for item in result["details"] if item["dimension"] == "画面美学" and item["id"] == "1"]
            self.assertTrue(all(item["result"] == "正确" for item in multi_details))
            self.assertTrue(all(item["standard_keywords"] == "都一般｜一样好" for item in multi_details))
            evidence = [item for item in result["standard_answer_evidence"] if item["dimension"] == "画面美学" and item["id"] == "1"]
            self.assertEqual(evidence[0]["raw_cell"], ["都一般", "一样好"])
            self.assertEqual(evidence[0]["keywords"], ["都一般", "一样好"])
            reverse_match = [item for item in result["details"] if item["student"] == "赵六" and item["dimension"] == "动态美学" and item["id"] == "2"]
            self.assertEqual(reverse_match[0]["result"], "正确")
            self.assertEqual(reverse_match[0]["matched_keywords"], "videoContrast好")
            workbook = load_workbook(xlsx, data_only=False)
            self.assertEqual(workbook.sheetnames, ["成绩汇总", "逐题明细", "异常复核", "证据索引"])
            self.assertIsInstance(workbook["成绩汇总"]["D2"].value, (int, float))
            self.assertGreater(len(workbook["成绩汇总"].conditional_formatting), 0)
            workbook.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
