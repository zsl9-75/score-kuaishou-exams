# 快手考试批改 Skill

这个 Skill 用来把标准答案和学员作业按“题目 ID + 评分维度表头”对齐，再生成正式评分 JSON、Excel 和色阶图。Docs 表格与截图考试是两条独立入口，但最终使用同一套评分规则。

## 安装

在另一台安装了 Codex 的电脑中，对 Codex 说：

```text
请使用 $skill-installer 从 https://github.com/zsl9-75/score-kuaishou-exams 安装仓库根目录的 Skill：path 使用 .，安装名使用 score-kuaishou-exams。
```

标准安装应保留 `scripts/`、`references/` 和 `agents/`。若目标平台仍会自动平铺，当前脚本也能从Skill根目录寻找同名配置和OCR资源；如需手动重建标准ZIP，运行根目录或 `scripts/` 下的 `package_skill.py`。

```bash
python3 scripts/package_skill.py
```

生成的 ZIP 会保留顶层 `score-kuaishou-exams/` 目录和所有子目录。Excel与PNG依赖可用下列方式安装：

```bash
python3 -m pip install -r requirements.txt
```

## 在 Codex 中怎么说

直接在请求里点名 Skill，并把输入与交付目录说清楚：

```text
使用 $score-kuaishou-exams 批改这次29组补考。标准答案是……，学员作业索引是……。Excel和色阶图请交付到 /Users/我的名字/Downloads/29组补考结果。
```

同一次考核继续运行时要复用原来的 Manifest 和 `task_id`；换一次考核就使用新的 `task_id`。Skill 会把首次学到的最小页签和范围只固化在本次考核中，后续同学复用，不会跨考核套用旧范围。

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

## 文件为什么不会再返回错目录

正式交付目录必须是绝对路径。程序最后会输出一个 JSON 行，其中 `json`、`xlsx`、`png` 才是实际生成文件的绝对路径。Skill 被要求在回复前再次确认这些文件存在，不再返回 Skill 安装目录、缓存目录、临时目录或输入文件目录。

如果有任何地方没有继续执行，最后一行还会包含 `stopped_items`。每一项都会写清停止位置、原因和下一步，调用Skill的Agent必须把这些内容完整告诉你，不能只返回一个“失败”状态。

更多细节见 [SKILL.md](SKILL.md)、[Manifest说明](references/manifest.md) 和 [证据格式](references/evidence-schema.md)。
