# 快手考试批改 Skill

这个 Skill 用来把标准答案和学员作业按“题目 ID + 评分维度表头”对齐，再生成正式评分 JSON、Excel 和色阶图。Docs 表格与截图考试是两条独立入口，但最终使用同一套评分规则。

## 安装

在另一台安装了 Codex 的电脑中，对 Codex 说：

```text
请使用 $skill-installer 从 https://github.com/zsl9-75/score-kuaishou-exams 安装仓库根目录的 Skill：path 使用 .，安装名使用 score-kuaishou-exams。
```

标准安装应保留 `scripts/`、`references/` 和 `agents/`。若目标平台自动平铺，根目录的 `resolve_skill.py` 会从自身位置识别实际安装根目录并返回正式脚本绝对路径，不需要用户知道安装位置。Excel与PNG依赖可用下列方式安装：

```bash
python3 -m pip install -r requirements.txt
```

## 在 Codex 中怎么说

直接在请求里点名 Skill，并把输入与交付目录说清楚：

```text
使用 $score-kuaishou-exams 批改这次29组补考。标准答案是……，学员作业索引是……。Excel和色阶图请交付到 /Users/我的名字/Downloads/29组补考结果。
```

同一次考核继续运行时要复用原来的 Manifest 和 `task_id`；换一次考核就使用新的 `task_id`。Skill 会把首次学到的最小页签和范围只固化在本次考核中，后续同学复用，不会跨考核套用旧范围。

如果标准表有两套同名 `ID` 列，或同一学员的画面美学、动态美学分别在两份文档中，不需要改原表。Manifest 可为标准答案声明逐维度列绑定，并为每份学员文档声明所属维度；评分器会按“姓名 + 维度 + 该维度ID”合并，证据与布局缓存仍分别保留。示例见 [Manifest说明](references/manifest.md)。

检查点还会绑定组别、考试配置、场次、标准答案和作业来源的身份指纹。如果误把29组的 `task_id` 用给30组，或把结业文生的 `task_id` 用给补考文生，程序会明确停止并要求创建新 `task_id`。文档 revision、学员增删、输出目录和并发数变化不会误触发该保护。

## 为什么不会再因中间格式错误直接停止

Docs 任务现在只走一个 `workflow` 状态机。Agent 不再手工选择“计划、入库、布局校验”等底层子命令，而是每次只提交程序返回的当前操作。

- 数组、`{"items": [...]}`、`item_id → revision` 和“文档ID → revision”都会自动转成标准快照。
- 少一项、多一项或某一项冲突时，其他合法文档照常继续，只补取问题项。
- 超时、`429` 和短暂 `5xx` 最多共3次尝试；布局变化自动回退到结构发现。
- 权限、文档不存在或学员身份冲突只隔离对应文档，其他学员继续。
- 重复提交同一 `operation_id` 不会重复计数或入库；进程中断后复用原 Manifest 和 `task_id` 即可从检查点续跑。
- Docs 原始读取默认每3份形成一个检查点，首份布局学习固定单份提交。当前批次完成后立即入库；上下文切换时最多重做当前小批次，不再等待整组全部读完才保存。

只要还有可执行动作，Skill 就会继续。自动恢复全部耗尽后，才会一次性把需要你处理的权限、标准答案空值、OCR复核等问题列出。若必需证据不完整，只保存可续跑的 `incomplete` JSON，不生成可能被误用的正式 Excel/PNG。

## 截图考试

标准答案和作业都可以是图片。图片文件名如果能确认是学员姓名就直接使用；否则会从图中的“姓名/学员姓名/同学名称”字段识别。建议同时提供学员名单，避免把“截图001”之类的文件名误判成人名。

Mac 默认使用系统 Vision OCR；非 Mac 默认使用视觉 API，并发处理截图。API 通过环境变量配置：

```bash
export SCORE_OCR_API_KEY="你的密钥"
export SCORE_OCR_API_MODEL="支持图片输入的模型名"
export SCORE_OCR_API_URL="https://api.openai.com/v1/responses"
```

密钥不会写入评分证据或仓库。运行结果会记录实际 OCR 耗时；要比较 Mac Vision 与 API，请对同一批图片分别指定 `vision` 和 `api`，不要使用固定经验倍率。

任何低置信度、缺失置信度或无法对齐的截图评分格都会进入全局待复核，不会生成正式Excel或PNG。

## 标准答案出现异常空值

`多镜头指令遵循`和`多镜头间一致连贯性`的标准空值正常参与评分：作业同样为空才正确。其他维度出现标准空值时，程序会在计算准确率前暂停、保留已读取证据，并一次列出全部异常位置。

此时请选择“审核标准答案”或“继续计算准确率”。选择继续后，把暂停JSON中的决策键传回原命令：

```bash
python3 scripts/run_assessment.py ... \
  --standard-blank-action exclude \
  --standard-blank-decision-key "返回的decision_key"
```

继续后只排除具体空值格，个人和全组准确率按剩余有效格汇总。决策键与当前标准答案版本绑定，标准答案变化后必须重新确认。

## 文件为什么不会再返回错目录

正式交付目录必须是绝对路径。程序最后会输出一个 JSON 行，其中 `json`、`xlsx`、`png` 才是实际生成文件的绝对路径。Skill 被要求在回复前再次确认这些文件存在，不再返回 Skill 安装目录、缓存目录、临时目录或输入文件目录。

如果有任何地方没有继续执行，最后一行还会包含 `stopped_items` 和归并后的 `user_actions`。每一项都会写清停止位置、原因和下一步，调用Skill的Agent必须把这些内容完整告诉你，不能只返回一个“失败”状态。

更多细节见 [SKILL.md](SKILL.md)、[Manifest说明](references/manifest.md) 和 [证据格式](references/evidence-schema.md)。
