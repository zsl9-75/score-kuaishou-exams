---
name: score-kuaishou-exams
description: 核对快手考试、补考和小考准确率，按固定维度与ID比较标准答案和学员作业。使用Manifest、批量Docs预检、同一考核首份作业范围学习与复用、15路并发、证据与评分缓存、失败续跑、多关键词双向包含匹配，并可选生成openpyxl Excel与色阶PNG。
---

# 快手考试准确率

使用 Manifest 编排 Docs 读取，使用脚本执行缓存、匹配、评分和输出。不要临时重写评分规则。

## 入口分流

1. 用户只要从已有正式JSON生成Excel或PNG时，直接执行 `--result-json`；不访问Docs，不重新评分。
2. 存在原Manifest时复用原 `task_id`，走revision增量路径。只读revision变化、上次失败或未完成的文档。
3. 只有首次处理本次考核时走完整取数路径。从用户请求确定考试配置、组别和进度，只使用 [exam_profiles.json](references/exam_profiles.json) 声明的维度。

## Manifest主流程

1. 创建或更新 [Manifest](references/manifest.md)。一次考核始终复用同一 `task_id`；不同考核使用新 `task_id`，禁止跨考核复用作业范围。
2. 先执行 `specs --stage initial` 取得稳定 `item_id`、文档ID和URL，不修改运行状态。再批量查询标准答案与作业索引的权限、revision、页签和范围，一次性生成预检快照。权限或不存在错误不重试；`429`/timeout/短暂`5xx`才退避重试。`runtime.retries=3` 表示最多3次总尝试（首次＋最多2次重试）。
3. 执行初始规划：

```bash
python3 scripts/manifest_runtime.py specs --manifest /path/to/task.json --stage initial
python3 scripts/manifest_runtime.py plan \
  --manifest /path/to/task.json \
  --stage initial \
  --revisions /path/to/initial-revisions.json
```

4. `cached` 不重读。`read` 并行读取最小范围，证据直接写入本地，Agent上下文只保留状态、计数、哈希和路径，不回显全部单元格。单份可执行 `ingest`，批量证据优先使用 `ingest-batch`：

```bash
python3 scripts/manifest_runtime.py ingest --manifest /path/to/task.json --item-id ITEM_ID --evidence /path/to/evidence.json
python3 scripts/manifest_runtime.py ingest-batch --manifest /path/to/task.json --evidence-dir /path/to/incoming
python3 scripts/manifest_runtime.py fail --manifest /path/to/task.json --item-id ITEM_ID --error "Docs timeout" --error-kind timeout
```

5. 作业索引 ingest 后，先用 `specs --stage students` 取得全部学员 `item_id`，再批量预检全部学员文档并规划取数：

```bash
python3 scripts/manifest_runtime.py specs --manifest /path/to/task.json --stage students
python3 scripts/manifest_runtime.py plan \
  --manifest /path/to/task.json \
  --stage students \
  --revisions /path/to/student-revisions.json
```

6. 本次考核没有已学习范围时，`plan` 只把第一份作业放入 `read`，其他放入 `deferred`。对首份作业做一次结构发现，将证据保存为实际最小范围并 ingest。状态会在当前 `task_id` 下固化页签、范围、必需表头和有效ID数。
7. 再次执行 `plan --stage students`。其余学员使用已学习范围，最多15路并发读取。每份都校验必需表头和ID数；证据仅页签/范围与计划不同时也视为布局失配，文档ID、revision或学员身份不同仍是致命metadata错误。快速Docs读取未能产生证据但已确认是布局变化时，执行 `fail --error-kind layout_mismatch`。这些情况均转为 `needs_discovery`，只让该文档回退结构发现。
8. 所有必需证据就绪后评分：

```bash
python3 scripts/run_assessment.py --manifest /path/to/task.json
```

`--refresh` 忽略证据缓存并重新读取。无 revision 的文档每次重读，再以内容哈希判断是否需要重新评分。缓存和续跑状态保存在 Manifest 旁的 `.score-cache`，不对用户交付，也不放入 Skill ZIP。

## Docs 读取约束

- 在标准答案和作业表中，自动寻找同时包含受支持的题目ID表头（如 `ID`/`order`）和规定维度的工作表。唯一候选自动选择；多个候选时停止并请用户确认。
- 在索引表中只读姓名列和作业链接列。一份多人文档或多份个人文档都可使用。
- 只读文档，不编辑内容、权限或分享设置。Docs 能力不可用时停止，不退化为网页截图 OCR。
- 每次读取保存来源链接、文档 ID、revision、工作表、范围、原始单元格、读取时间和内容哈希，格式见 [evidence-schema.md](references/evidence-schema.md)。

## 截图考试

截图考试的图片基本文件名必须是学员姓名，每人一张。macOS 优先调用 Vision OCR；非 macOS 先用平台原生批量 OCR 生成相同证据结构。仅并发处理截图作业，标准答案仍必须是 Docs 结构化证据。

## 输出模式

默认只原子保存 `组别_进度_评分结果.json`（schema v2），之后才选择渲染：

```bash
# 只评分并输出文字摘要，强制不生成PNG/Excel
python3 scripts/run_assessment.py --manifest /path/to/task.json --summary-only

# 评分时按需生成
python3 scripts/run_assessment.py --manifest /path/to/task.json --png on --xlsx auto

# 从现有评分JSON重新生成，不访问Docs、不重新评分
python3 scripts/run_assessment.py --result-json /path/to/result.json --output /path/to/output --png on --xlsx on
```

- `--png on|off` 默认 `off`。
- `--xlsx auto|on|off` 默认 `off`。`auto` 在 openpyxl 不可用时只警告；`on` 输出失败时保留 JSON 和其他成功文件，并以输出失败状态结束。
- `--skip-xlsx` 暂时等价于 `--xlsx off`。CLI 覆盖 Manifest，Manifest 覆盖默认值。
- 评分 JSON 始终先保存。任一必需文档失败时，保存 `incomplete` JSON，暂停正式 PNG/Excel，退出码为 4；再次运行同一 Manifest 仅补读失败、未完成或 revision 变化的文档。

## 不可变评分规则

- 按规范化 ID 和表头匹配，禁止按物理行序或列序匹配。未声明维度、归因列和解释列不计分。
- 以标准答案每个维度的 ID 集合为题目全集。缺失 ID 计错，额外 ID 只进入异常，重复 ID 中止该数据集。
- `多镜头指令遵循`、`多镜头间一致连贯性` 允许标准空值：空/空正确，空/非空错误，非空/空错误。其他维度标准空值中止，作业空值计错。
- 标准答案原始单元格是唯一依据，不限制为预设值，不改写、补字或替换。一个单元格可包含一个或多个关键词，多标签/数组或换行、逗号、顿号、斜杠、竖线都可分隔。重复关键词去重；结构化空值不得与非空关键词混合。
- 作业答案不做规范值转换。仅在匹配副本中去除空白，按区分大小写的双向包含判定：作业包含标准关键词，或标准关键词包含作业。命中任意关键词即正确，该 ID/维度始终只计一道题。
- 多维综合准确率是规定维度的等权平均，`整体` 也正常参与平均。

## 输出验收

- JSON schema v2 包含完整汇总、逐题明细、异常、证据索引、失败文档、缓存命中与评分规则版本。
- Excel 使用 openpyxl，包含 `成绩汇总`、`逐题明细`、`异常复核`、`证据索引` 四个工作表；准确率是数值，使用 Excel 原生条件格式。
- 单维 PNG 按准确率降序显示同学、答对数、准确率和错误 ID；多维 PNG 按配置顺序显示各维度与等权平均。
- 色阶固定：90–100% `#6AB37B`，80–89% `#A7D08D`，70–79% `#FEE07B`，60–69% `#F5A05C`，低于60% `#F35161`。
