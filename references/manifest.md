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
    "checkpoint_batch_size": 3,
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

## task_id 身份绑定

一个 `task_id` 只代表一个组别的一次具体考试。首次写入检查点时，运行时会保存 `manifest_identity`，其指纹只包含：

- `group`：当前组别，例如 `29组`；
- `exam_profile`：结业文生、结业图生、补考文生等考试配置；
- `progress`：第一次、第二次或其他用于区分场次的进度名称；
- 标准答案的文档 ID，或截图/本地证据来源；
- 作业索引文档 ID 或作业来源模式。

下列字段不进入身份指纹，因为它们可以在同一考试续跑时合法变化：文档 `revision`、学员增删、输出目录、并发数和重试参数。

如果同一 `task_id` 的组别、考试配置、场次、标准答案或作业来源发生变化，CLI返回 `error_code=manifest_identity_mismatch` 并停止；为新组别/新考试创建新 `task_id`。`--refresh` 只能刷新同一任务的权限、revision和证据，不会清除身份绑定。旧版检查点首次读取时自动迁移；如果已记录的标准答案文档与当前 Manifest 不同，迁移也会停止。

## 统一workflow接口

Agent默认只调用 `capabilities --json` 和 `workflow`。`specs`、`plan`、`ingest`、`ingest-batch`、`fail`与`status`保留为兼容/调试接口，禁止Agent在任务中猜测新的底层命令。

```bash
python3 scripts/manifest_runtime.py capabilities --json
python3 scripts/manifest_runtime.py workflow --manifest /path/to/task.json
```

`workflow` 每次返回 `workflow_status`、`operation_id`、`action`、`recoverable`、`blocked_items` 和 `user_actions`：

- `preflight_docs` 仍可批量检查全部待读文档，因为它只返回权限、revision和范围等紧凑元数据。
- `read_docs` 按 `runtime.checkpoint_batch_size` 切成耐中断小批次；默认每批3份，布局探测或布局回退固定为1份。
- 当前小批次每份证据落盘后立即提交。允许只提交批次中已经完成的部分，未回传项会保留并出现在下一次 `read_docs`，不会被判定为失败。
- `action.effective_concurrency` 只控制当前小批次并发，不能超过批次数或 `runtime.max_concurrency`。

- `action_required + preflight_docs`：填充 `action.response_template` 后使用 `--response`回传。
- `action_required + read_docs`：将证据保存为 `<item_id>.json`，使用 `--evidence-dir` 回传；读取失败可同时放在 `--response`。
- `retrying`：到 `next_attempt_at` 后再调用同一命令。
- `ready_to_score + score`：执行评分，再使用 `--result-json` 和该 `operation_id` 回传结果。
- `awaiting_user`或`complete`：终态。仅前者需一次性询问 `user_actions`。
- `engineering_blocked`：同一本地契约错误连续3次后才允许终止，状态和证据保留。

`operation_id` 是幂等键。重复回传已应用的操作只返回恢复说明，不重复计数或入库。过期 `operation_id`或`specs_hash`不得应用，运行时返回当前操作的正确模板续跑。

## 批量预检快照

workflow v2 的标准预检快照为：

```json
{
  "schema_version": 2,
  "task_id": "group-29-quiz-text-to-video",
  "stage": "students",
  "operation_id": "operation-id",
  "specs_hash": "specs-hash",
  "items": [
    {"item_id": "ITEM_A", "revision": "r15", "sheet": "作业", "range": "A1:AB11"}
  ]
}
```

运行时同时兼容顶层数组、`{"items": [...]}`、`item_id → revision`和“文档ID → revision”映射。文档ID只有在本轮唯一匹配时才自动转换；歧义、冲突或缺失项只重取对应文档，其他合法项继续。revision为空不是致命错误，但会禁用revision缓存并每次重读。

以下底层快照说明仅用于兼容和调试。

预检前先获取不修改状态的文档清单：

```bash
python3 scripts/manifest_runtime.py specs --manifest /path/to/task.json --stage students
```

输出的 `items` 已包含稳定 `item_id`、文档ID和URL，批量Docs预检应原样回传 `item_id`，避免Agent自行重算或逐份建立对应。此处的旧 `plan --revisions` 接口仍要求快照完整、唯一且只含当前项；统一 `workflow` 接口不会整批拒绝，它会保留合法项并只重取缺失、歧义或冲突项。

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
- `runtime.checkpoint_batch_size` 默认 3，只允许 1–5。它决定每次 `read_docs` 最多读取并落盘多少份证据，与并发上限分开；批次越小，中断时可能重读的文档越少。
- `runtime.retries` 默认 3，表示最多3次总尝试（首次＋最多2次重试）；对应退避数组默认为 1、2 秒。
- `runtime.learn_homework_layout` 默认 `true`；可由 `homework.layout_reuse.enabled` 覆盖。
- `output.png` 是 `on|off`，默认 `off`。
- `output.xlsx` 是 `auto|on|off`，默认 `off`。
- `output.dir` 必须是绝对交付目录；推荐每次显式传绝对 `--output`，避免返回Skill目录或临时目录。
- 每次运行在 `output.dir` 下生成独立的 `组别_进度__run_id/` 原子发布目录；正式JSON中的绝对路径是本次唯一交付依据。
- `cache.dir` 默认 `.score-cache`，必须留在本地运行目录，不对用户交付，也不提交到 Skill 仓库。
- `runtime.ocr_workers` 是API OCR并发数，默认4，只允许1–8；`runtime.ocr_confidence_threshold` 默认0.75。
- `runtime.ocr_engine` 是 `auto|vision|api`；`auto` 在macOS使用Vision，其他系统使用API。
- 截图标准答案与作业都必须逐题提供有效OCR置信度；任何待复核项都会停止正式PNG/Excel并写入 `stopped_items`。
- 非特殊维度标准空值触发 `awaiting_standard_decision`（退出码6），保留全部证据缓存并暂停评分。用户选择审核后复用同一 `task_id`；选择继续时向 `run_assessment.py` 同时传 `--standard-blank-action exclude` 和暂停JSON中的 `--standard-blank-decision-key`。键与标准答案版本不匹配时重新暂停，禁止沿用旧授权。
- 作业索引变化后，已删除学员标记为 `removed`，新增或链接变化的学员创建新读取项，未变学员继续复用缓存。
