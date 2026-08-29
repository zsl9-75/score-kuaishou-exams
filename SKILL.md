---
name: score-kuaishou-exams
description: 核对快手考试、补考和小考准确率，按固定维度与ID比较标准答案和学员作业。支持Docs结构化证据、截图标准答案与截图作业、Mac Vision或并发视觉API OCR、同一考核范围学习复用、缓存续跑、多关键词双向包含匹配，并生成正式JSON及可选Excel和色阶PNG。
---

# 快手考试准确率

使用 Manifest 编排 Docs 读取，使用脚本执行缓存、匹配、评分和输出。不要临时重写评分规则。

优先使用 `scripts/run_assessment.py` 与 `scripts/manifest_runtime.py`。若目标平台把Skill平铺且没有 `scripts/`，使用Skill根目录下同名脚本；脚本会自动从根目录寻找配置和OCR资源。不要自行猜测其他路径。

## 入口分流

1. 用户只要从已有正式JSON生成Excel或PNG时，直接执行 `--result-json`；不访问Docs，不重新评分。
2. 存在原Manifest时复用原 `task_id`，走revision增量路径。只读revision变化、上次失败或未完成的文档。
3. 截图考试使用 `screenshot-*` 配置；一次性运行可直接走 `--standard-images + --images`，需要固定输入与OCR配置时也可使用截图Manifest。
4. 只有首次处理本次Docs考核时走完整取数路径。从用户请求确定考试配置、组别和进度，只使用 [exam_profiles.json](references/exam_profiles.json) 声明的维度。

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
python3 scripts/run_assessment.py --manifest /path/to/task.json --output /absolute/delivery/path
```

`--refresh` 忽略证据缓存并重新读取。无 revision 的文档每次重读，再以内容哈希判断是否需要重新评分。缓存和续跑状态保存在 Manifest 旁的 `.score-cache`，不对用户交付，也不放入 Skill ZIP。

## Docs 读取约束

- 在标准答案和作业表中，自动寻找同时包含受支持的题目ID表头（如 `ID`/`order`）和规定维度的工作表。唯一候选自动选择；多个候选时停止并请用户确认。
- 在索引表中只读姓名列和作业链接列。一份多人文档或多份个人文档都可使用。
- 只读文档，不编辑内容、权限或分享设置。Docs 能力不可用时停止，不退化为网页截图 OCR。
- 每次读取保存来源链接、文档 ID、revision、工作表、范围、原始单元格、读取时间和内容哈希，格式见 [evidence-schema.md](references/evidence-schema.md)。

## 截图考试

标准答案和学员作业都可以是截图。先把OCR结果转换为与Docs一致的 `headers + rows` 证据，再按ID和表头匹配评分；表头允许与配置别名形成双向包含关系，归因、解释和备注列不参与评分。

学员姓名按以下顺序确定：

1. 如果提供 `--student-roster`，只有文件名规范化后命中名单才视为姓名。
2. 没有名单时，仅把2–4个汉字且不含“截图/图片/image/screenshot”等通用词的文件名视为姓名。
3. 文件名不是姓名时，从截图内的“同学名称/同学姓名/学员姓名/姓名”字段识别。
4. 文件名姓名与图内姓名冲突、姓名不在名单中或多张图映射到同一人时中止，禁止静默猜测。

macOS 的 `--ocr-engine auto` 使用系统 Vision；非 macOS 的 `auto` 使用视觉API，并用 `--ocr-workers` 进行1–8路并发。API密钥只从环境变量读取，不写入Manifest、证据、日志或仓库：

```bash
export SCORE_OCR_API_KEY="..."
export SCORE_OCR_API_MODEL="支持图片输入的模型名"
# 默认是 https://api.openai.com/v1/responses；OpenAI兼容服务可覆盖
export SCORE_OCR_API_URL="https://api.openai.com/v1/responses"

python3 scripts/run_assessment.py \
  --exam-profile screenshot-image-aesthetic \
  --group "29组" \
  --progress "画面美学" \
  --standard-images /absolute/input/standard.png \
  --images /absolute/input/homework \
  --student-roster /absolute/input/students.json \
  --ocr-engine auto \
  --ocr-workers 4 \
  --output /absolute/delivery \
  --png on --xlsx on
```

要比较Mac Vision与API耗时，在同一台Mac、同一批图片上分别执行 `--ocr-engine vision` 和 `--ocr-engine api`。结果JSON的 `metadata.ocr.standard/homework.elapsed_seconds` 与 `per_image_seconds` 是实测值；API耗时受模型、网络、限流、图片大小和并发数影响，不写死固定倍率。

截图标准答案和截图作业的每个有效ID都必须有0–1有限数值置信度。标准答案或作业出现低置信度、缺失、非法值、重复键或规范化后无法对齐时，相关评分格进入“待复核”；全局状态为 `pending_review`，退出码为5，禁止生成正式PNG和Excel。

## 输出模式

默认保存 schema v4 评分JSON，之后才选择渲染：

```bash
# 只评分并输出文字摘要，强制不生成PNG/Excel
python3 scripts/run_assessment.py --manifest /path/to/task.json --output /absolute/delivery --summary-only

# 评分时按需生成
python3 scripts/run_assessment.py --manifest /path/to/task.json --output /absolute/delivery --png on --xlsx auto

# 从现有评分JSON重新生成，不访问Docs、不重新评分
python3 scripts/run_assessment.py --result-json /path/to/result.json --output /absolute/delivery --png on --xlsx on
```

非特殊维度出现标准空值时，第一次运行返回 `awaiting_standard_decision`、退出码6和 `decision_request`，不得继续评分。必须一次列出全部空值并问用户选择“审核标准答案”或“继续计算准确率”。用户明确选择继续后，使用原命令加上：

```bash
--standard-blank-action exclude \
--standard-blank-decision-key "暂停JSON中的decision_key"
```

决策键绑定完整标准答案内容和异常空值位置；标准答案发生任何变化时旧键自动失效。用户选择审核时等待其修订，之后复用同一Manifest和 `task_id`，不要重读未变化的学员证据。

- `--png on|off` 默认 `off`。
- `--xlsx auto|on|off` 默认 `off`。`auto` 在 openpyxl 不可用时只警告；`on` 输出失败时回滚本次所有正式PNG/XLSX，只保留说明原因的 `output_failed` JSON，退出码为3。
- `--skip-xlsx` 暂时等价于 `--xlsx off`。CLI 覆盖 Manifest，Manifest 覆盖默认值。
- 每次运行使用独立 `run_id` 子目录并在临时目录生成，全部成功后原子发布；不同运行不共享同名正式文件。任一必需文档失败时保存 `incomplete` JSON，暂停正式PNG/Excel，退出码为4；再次运行同一Manifest仅补读失败、未完成或revision变化的文档。
- `--output` 必须是绝对交付目录；Manifest 的 `output.dir` 只有写成绝对路径时才可替代。不要把产物写进Skill安装目录、缓存目录或临时目录。
- 最终回复只使用CLI最后一行JSON里的 `json/xlsx/png` 绝对路径，并在回复前确认文件存在。不要手写、推测或返回输入证据目录。

## 未继续项硬性回报

每次运行都读取最终CLI JSON中的 `stopped_items`。只要列表非空，最终回复必须逐条告诉用户：停止阶段、学员/维度/ID、`reason` 和 `next_action`；不能只说“失败”“待复核”或“稍后继续”。

- `awaiting_standard_decision`、`pending_review`、`incomplete`、`output_failed` 绝不能表述为已完成。
- `awaiting_standard_decision` 时必须逐项列出异常维度和ID，明确说明已保留读取数据，并原样提供“审核标准答案”和“继续计算准确率”两个选择；未经用户明确选择禁止自行排除。
- CLI在输入、权限、证据结构或环境错误时，也会在stderr最后一行输出 `status=stopped` 和停止原因；最终回复必须转述。
- 即使主任务有部分成功，也必须报告所有没有继续执行的分支及停止原因。
- 只有 `run_status=complete` 且没有未说明的停止项时，才能交付正式成绩。

## 不可变评分规则

- 按规范化 ID 和表头匹配，禁止按物理行序或列序匹配。未声明维度、归因列和解释列不计分。
- 以标准答案每个维度的 ID 集合为题目全集。缺失 ID 计错，额外 ID 只进入异常，重复 ID 中止该数据集。
- `多镜头指令遵循`、`多镜头间一致连贯性` 允许标准空值：空/空正确，空/非空错误，非空/空错误，始终计入分母。
- 其他维度标准空值在任何准确率计算前触发决策门禁。用户选择继续后只排除对应评分格，不判对错且不进入维度、个人或全组分母；整维全部排除时显示“未评分”。若所有维度都无有效格则停止正式交付并要求审核标准答案。
- 标准答案原始单元格是唯一依据，不限制为预设值，不改写、补字或替换。一个单元格可包含一个或多个关键词，多标签/数组或换行、逗号、顿号、斜杠、竖线都可分隔。重复关键词去重；结构化空值不得与非空关键词混合。
- 作业答案不做规范值转换。仅在匹配副本中去除空白，按区分大小写的双向包含判定：作业包含标准关键词，或标准关键词包含作业。命中任意关键词即正确，该 ID/维度始终只计一道题。
- 个人总准确率和全组准确率均按“正确有效格数 ÷ 有效评分格总数”计算；被排除格完全不参与，`整体`仍是普通评分维度。

## 输出验收

- JSON schema v4 包含完整汇总、逐题明细、异常、证据索引、失败文档、`group_summary`、`run_id`、`stopped_items`、缓存命中与评分规则版本。排除格记录为“不计分”，维度同时记录原始题数 `source_total`、有效分母 `total` 和 `excluded_ids`；旧schema仍可读取。
- Excel 使用 openpyxl，包含 `成绩汇总`、`逐题明细`、`异常复核`、`证据索引` 四个工作表；准确率是数值，使用 Excel 原生条件格式。
- 单维 PNG 按准确率降序显示同学、有效答对数、准确率及错误/排除ID；多维 PNG 按配置顺序显示各维度与有效格汇总总准确率。
- 色阶固定：90–100% `#6AB37B`，80–89% `#A7D08D`，70–79% `#FEE07B`，60–69% `#F5A05C`，低于60% `#F35161`。
