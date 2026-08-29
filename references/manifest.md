# Manifest JSON

Manifest 内的证据、别名和缓存相对路径都相对于 Manifest 所在目录解析。正式 `output.dir` 必须是绝对目录，也可以在运行时用绝对 `--output` 覆盖。建议同一批考试持续复用 `task_id`。

```json
{
  "schema_version": 1,
  "task_id": "group-29-quiz-text-to-video",
  "exam_profile": "quiz-text-to-video",
  "group": "29组",
  "progress": "小考文生",
  "standard": {
    "url": "https://docs.corp.kuaishou.com/standard",
    "id": "standard-document-id",
    "sheet": "标准答案",
    "range": "A1:L200"
  },
  "homework": {
    "index": {
      "url": "https://docs.corp.kuaishou.com/homework-index",
      "id": "homework-index-document-id",
      "sheet": "作业索引",
      "range": "A1:C100",
      "name_header": "同学名称",
      "link_header": "作业链接"
    },
    "document_defaults": {
      "sheet": "作业"
    },
    "layout_reuse": {
      "enabled": true,
      "discovery_range": "A1:AB200"
    }
  },
  "name_aliases": "name-aliases.json",
  "output": {
    "dir": "/absolute/path/to/delivery",
    "png": "off",
    "xlsx": "off"
  },
  "cache": {
    "dir": ".score-cache"
  },
  "runtime": {
    "max_concurrency": 15,
    "retries": 3,
    "retry_delays_seconds": [1, 2],
    "ocr_workers": 4,
    "ocr_confidence_threshold": 0.75
  }
}
```

`homework.index` 可替换为显式 `homework.documents` 数组：

```json
{
  "homework": {
    "documents": [
      {"student": "张三", "url": "https://docs.corp.kuaishou.com/zhang-san"},
      {"student": "李四", "url": "https://docs.corp.kuaishou.com/li-si"}
    ]
  }
}
```

截图考试使用 `screenshot-*` 配置时，Manifest 不执行Docs发现读取，直接读取图片：

```json
{
  "schema_version": 1,
  "task_id": "group-29-screenshot-aesthetic-2026-08",
  "exam_profile": "screenshot-image-aesthetic",
  "group": "29组",
  "progress": "画面美学截图考试",
  "standard": {"images": "/absolute/input/standard.png"},
  "homework": {
    "images": "/absolute/input/homework",
    "student_roster": "/absolute/input/students.json"
  },
  "output": {"dir": "/absolute/delivery", "png": "on", "xlsx": "on"},
  "runtime": {
    "ocr_engine": "auto",
    "ocr_workers": 4,
    "ocr_confidence_threshold": 0.75
  }
}
```

`standard.images` 与 `homework.images` 可以是单张图片或图片文件夹；也可分别改为已经生成的 `source=image_ocr` 证据路径 `evidence`。图片路径与证据路径必须二选一。

Agent 读取 Docs 后，可在对应文档对象中临时填写 `revision`、`sheet`、`range` 或在 `plan --revisions` 快照中提供。`evidence` 只用于已有本地证据 JSON 的预读/测试流程。

`homework.layout_reuse.enabled=true` 时，不要在 `document_defaults.range` 中固定跨考核范围。`discovery_range` 只是首份作业的发现上界；首份证据必须记录实际最小范围。该范围仅保存在当前 `task_id` 的 `.score-cache/runs/`状态中，同一考核后续学员复用；新考核使用新 `task_id` 会重新学习。

## 批量预检快照

预检前先获取不修改状态的文档清单：

```bash
python3 scripts/manifest_runtime.py specs --manifest /path/to/task.json --stage students
```

输出的 `items` 已包含稳定 `item_id`、文档ID和URL，批量Docs预检必须原样回传 `item_id`，避免Agent自行重算或逐份建立对应。快照必须完整覆盖本次 `specs` 的全部项，不能出现重复、未知或空 `item_id`；否则整批拒绝，防止部分预检被误当成全量成功。

权限、revision和候选页签应在同一批预检中取得。标准答案和索引可同时提供已确认范围；显式开启 `layout_reuse` 的学员项即使快照带有大范围，运行时也会将它仅作为发现信息，强制用首份实际最小范围建立模板。成功项与失败项使用同一快照：

```json
{
  "items": [
    {"item_id": "ITEM_A", "revision": "r15", "sheet": "作业"},
    {"item_id": "ITEM_B", "error_kind": "permission", "error": "无权访问"},
    {"item_id": "ITEM_C", "error_kind": "timeout", "error": "Docs timeout"}
  ]
}
```

`permission`/`not_found`/`other` 直接进入 `failed`；`429`/`timeout`/`transient_5xx` 进入 `retry`。一次性列出全部无权文档，不要边正式读取边逐个发现权限问题。

## 同一考核的范围学习

首次 `plan --stage students` 可返回：

- `read`：仅1份 `read_mode=discovery_probe` 作业；
- `deferred`：等待首份范围固化的其他学员；
- `failed`：权限等不可重试失败；
- `retry`：可退避重试的预检或读取失败。

首份成功 ingest 后，再次执行同一 plan。其余学员会以 `read_mode=learned_fast` 读取已学习页签和范围。快速证据必须通过考试配置的ID、正式维度表头及首份有效ID数校验；失败时状态为 `needs_discovery`，下次plan仅让该文档以 `discovery_fallback` 回退。

如果快速Docs读取因页签或范围已失效而无法生成证据，使用：

```bash
python3 scripts/manifest_runtime.py fail --manifest /path/to/task.json --item-id ITEM_ID --error "已学习范围失效" --error-kind layout_mismatch
```

该错误不做原范围重试，而是直接转为 `needs_discovery`。只有页签/范围不同可走此回退；文档ID、revision或学员身份不同仍中止该证据。

## 批量证据入库

将每份证据命名为 `<item_id>.json`，放入同一临时目录，再一次执行：

```bash
python3 scripts/manifest_runtime.py ingest-batch \
  --manifest /path/to/task.json \
  --evidence-dir /path/to/incoming
```

命令只返回成功数、失败数、需回退数、缺失文件和紧凑结果；不向Agent回显全部单元格。

字段规则：

- `runtime.max_concurrency` 默认 15，只允许 1–15。
- `runtime.retries` 默认 3，表示最多3次总尝试（首次＋最多2次重试）；对应退避数组默认为 1、2 秒。
- `runtime.learn_homework_layout` 默认 `true`；可由 `homework.layout_reuse.enabled` 覆盖。
- `output.png` 是 `on|off`，默认 `off`。
- `output.xlsx` 是 `auto|on|off`，默认 `off`。
- `output.dir` 必须是绝对交付目录；推荐每次显式传绝对 `--output`，避免返回Skill目录或临时目录。
- 每次运行在 `output.dir` 下生成独立的 `组别_进度__run_id/` 原子发布目录；正式JSON中的绝对路径是本次唯一交付依据。
- `cache.dir` 默认 `.score-cache`，必须留在本地运行目录，不进入交付 ZIP。
- `runtime.ocr_workers` 是API OCR并发数，默认4，只允许1–8；`runtime.ocr_confidence_threshold` 默认0.75。
- `runtime.ocr_engine` 是 `auto|vision|api`；`auto` 在macOS使用Vision，其他系统使用API。
- 截图标准答案与作业都必须逐题提供有效OCR置信度；任何待复核项都会停止正式PNG/Excel并写入 `stopped_items`。
- 作业索引变化后，已删除学员标记为 `removed`，新增或链接变化的学员创建新读取项，未变学员继续复用缓存。
