# 证据 JSON 约定

在线文档必须通过有权限的 Docs 表格读取能力取得原始单元格，再保存为以下结构。截图考试由本Skill的OCR分支生成 `source=image_ocr` 的同构证据；不要把网页渲染文字伪装成Docs证据。

```json
{
  "schema_version": 1,
  "source": "docs",
  "document": {
    "url": "https://docs.corp.kuaishou.com/...",
    "id": "document-id",
    "revision": "revision-or-empty"
  },
  "sheet": "工作表名称",
  "range": "A1:L100",
  "read_at": "2026-08-23T12:00:00+08:00",
  "headers": ["同学名称", "ID", "基础画质"],
  "rows": [
    ["张三", 46, "videoContrast好"]
  ]
}
```

标准答案可没有“同学名称”列。作业证据必须包含姓名列；如果一份文档只属于一名学员，也可在顶层提供 `student_name`，但不能同时与姓名列产生冲突。

截图证据的 `document.id` 是图片文件名，`document.revision` 是图片SHA-256；`sheet` 是图片名，`range` 记录识别出的ID与目标维度。标准答案截图不含 `student_name`，作业截图必须在文件名判断或图内姓名OCR后写入 `student_name`。`ocr` 字段记录引擎、并发数和实测耗时；`confidence` 按规范化ID记录0–1置信度。

标准答案单元格可保存一个或多个可接受答案。必须保留 Docs 返回的原始结构，不要在读取证据时预先拼接或覆盖：

```json
{
  "headers": ["ID", "画面美学"],
  "rows": [
    [46, ["都一般", "一样好"]],
    [54, {"values": [{"text": "videoExp"}, {"text": "videoContrast"}]}],
    [70, "一样好／一样差"]
  ]
}
```

评分脚本也兼容换行、中英文逗号、顿号、半角或全角斜杠、半角或全角竖线分隔的文本。标准答案不设规范值白名单，每个拆分结果直接作为匹配关键词并按出现顺序去重。数组或多标签中明确出现空项且同时包含非空关键词属于配置错误；字符串分隔产生的空片段只作为格式噪声忽略。多关键词单元格仍只代表一个 ID/维度题目并只计一次分母。

匹配时保留标准答案原文不变，只在临时匹配副本中去除空白。对每个标准关键词执行双向包含：作业原文包含标准关键词，或标准关键词包含作业原文，任一方向成立都正确。匹配区分大小写，不补“好”、不做五值归一。例如标准为 `videoContrast`、作业为 `videoContrast好`时正确；标准为 `videoContrast好`、作业仅为 `videoContrast`时也正确。

结构化标签对象同时含内部 `value` 和可见 `text`、`label`、`name` 或 `title` 时，关键词使用可见文字；内部值仅在没有可见文字时作为后备。整个原始对象仍保存在 `raw_cell` 中。

多份作业可以分别保存为多个 JSON 并重复传入 `--homework-evidence`，也可保存为集合：

```json
{
  "schema_version": 1,
  "documents": [
    {"source": "docs", "headers": ["同学名称", "ID", "画面美学"], "rows": []},
    {"source": "docs", "headers": ["同学名称", "ID", "画面美学"], "rows": []}
  ]
}
```

约束：

- `headers` 与每行 `rows` 按列位置对应，但评分阶段只通过表头名称取列。
- 保留空单元格为 `null` 或空字符串，不删除含特殊允许空值的题目。
- 保留标准答案单元格的原始字符串、数组或标签对象；解析后的关键词集合由评分脚本生成，不得替换原始单元格。
- 保留 ID 原值，不在读取阶段排序或补齐。
- `document.url`、`id`、`revision`、`sheet`、`range` 用于审计和缓存；没有 revision 时保留空字符串。
- 不读取与目标表格无关的工作表和范围。

评分脚本的 schema v4 结果 JSON 会在 `standard_answer_evidence` 中逐个记录 `dimension`、规范化 `id`、`raw_cell`、`keywords`、`standard_blank`、标准答案OCR置信度和原始行号；并包含：

- `run_status`：`complete`、`incomplete`、`pending_review`、`output_failed` 或 `awaiting_standard_decision`；
- `summary`、`details`、`anomalies`：汇总、逐题明细和异常；
- `group_summary`：全组正确有效格、有效格总数和准确率；
- `evidence`：文档 revision、工作表、范围和内容哈希索引；
- `failed_documents`：失败或等待续跑的文档；
- `stopped_items`：每个未继续分支的阶段、对象、明确原因和下一步；
- `user_actions`：按停止阶段归并的最终用户处理清单，`items` 必须完整覆盖 `stopped_items`；
- `cache_stats`：证据和学员评分片段的命中/未命中数；
- `metadata.scoring_rule_version`、`standard_hash`、`profile_hash`、`aliases_hash`：增量评分依据。

非特殊维度出现标准空值时，结果必须在评分前进入 `awaiting_standard_decision`：

- `decision_request.decision_key` 绑定标准答案 revision、完整内容与异常空值位置；
- `affected_cells` 一次列出全部维度、ID、原始行号与单元格；
- `summary/details` 为空，表示尚未计算任何学员准确率；
- `preflight_review_items` 同时保留能在评分前确定的OCR待复核项，并与证据失败、标准空值一起写入 `stopped_items`；
- `evidence` 和 `standard_answer_evidence` 保留已读取数据，正式Excel/PNG为空；
- 用户确认继续后，正式结果以 `standard_blank_decision` 固化授权，明细结果为 `不计分`，维度通过 `source_total`、`total` 和 `excluded_ids` 审计实际分母。

个人与全组准确率按全部有效评分格汇总，不再对有效题数不同的维度做等权平均。整维无有效格时状态为“未评分”；全部维度无有效格时结果为 `incomplete`。

结果 JSON 是正式输出和后续渲染的唯一数据源。使用 `--result-json` 生成 PNG/Excel 时不再访问 Docs，不再评分。

## Workflow v2 检查点

Docs 取数不再由 Agent 手工组合中间状态。`manifest_runtime.py workflow` 在 Manifest 旁的 `.score-cache/runs/` 原子保存：

- `task_id`、当前 `stage`、`operation_id` 和 `specs_hash`；
- 本轮要预检或读取的 `items`；
- 已应用操作、外部重试次数、自动恢复记录和永久失败项；
- 已成功证据的缓存路径和内容哈希。

同一 `operation_id` 重复提交必须幂等：不重复增加尝试次数，不重复入库。过期的 `task_id`、`operation_id` 或 `specs_hash` 只返回可恢复契约错误，不改写当前操作。无 revision 证据不使用 revision 缓存，但读取后仍以完整内容哈希审计。

部分文档永久失败时，已成功学员可写入 `run_status=incomplete` 的检查点 JSON，但 `xlsx` 和 `png` 必须为空。只有全部必需证据有效且评分状态完整时，才能发布正式 Excel/PNG。

`source=image_ocr` 时，标准答案和作业的每个有效ID都必须在 `confidence` 中有对应的0–1有限数值。键按题目ID同规则规范化；缺失、布尔值、字符串、NaN、Infinity、越界、重复或无法对齐均不得静默计分，必须进入待复核或在API边界明确停止。

## 缓存键与续跑状态

- revision 存在时，证据缓存键是 `role + student + document_id + revision + sheet + range` 的稳定哈希，禁止不同学员共享同一证据缓存项。
- revision 为空时，不使用 revision 缓存盲目跳过读取；读取后以完整证据内容哈希保存。
- 学员评分缓存键包含标准答案内容哈希、该学员作业内容哈希、考试配置哈希、评分规则版本、姓名别名哈希、标准空值处理策略和decision key。
- 任务状态以文档为单位记录 `pending/cached/success/failed/removed`、尝试次数、错误和缓存路径。每份证据成功后立即原子保存证据与状态。
