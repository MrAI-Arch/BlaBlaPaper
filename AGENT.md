# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

BlaBlaPaper 是一个命令行工具，输入一篇英文论文 PDF，输出三份结构化的 Markdown 报告（技术深挖、通俗讲解、图表详解），并可选择导出为静态 HTML 网页。PDF 解析后端使用 MinerU 服务，分析部分调用 OpenAI 兼容的 LLM。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 分析 PDF（通过 MinerU 解析，然后生成三份报告）
python main.py /path/to/paper.pdf

# 同时导出 HTML
python main.py /path/to/paper.pdf --html

# 从已有报告重新导出 HTML（不调用 LLM）
python main.py /path/to/outputs/paper-title --html-only
```

项目暂无测试、lint 或构建步骤。

## 操作规范

- 运行工作流时**不要重定向 stdout/stderr 到文件**（如 `> log.txt 2>&1`），直接让输出打到终端。脚本内部会自动在输出目录保存 `run-*.log` 日志文件，无需额外重定向。

## 架构

整体流水线分为三个阶段：

### 1. PDF → Markdown（`src/mineru_client.py` + `src/parser.py`）

- MinerU API 上传 PDF，轮询等待解析完成，返回一个 ZIP 包，内含 `.md` 文件、图片和 `*_content_list.json` 清单文件。
- `parser.py` 从清单中提取图片引用、论文标题、图表说明、PDF 元数据（通过 PyMuPDF）以及被丢弃的文本块。

### 2. LLM 分析（`src/core.py` + `src/llm_client.py` + `src/prompts.py`）

- `core.py` 编排分析流程：论文基本信息 → 摘要 → 背景与贡献 → 技术点提取 → 逐点深挖 → 逐点通俗讲解 → 实验分析 → 逐图分析。
- `llm_client.py` 支持两种线协议，通过 `wire_api` 配置项切换：
  - `responses` —— OpenAI Responses API（`/responses` 端点，`input` + `instructions` 负载）
  - `chat_completions` —— 标准 `/chat/completions` 端点
- 内容块（文本、`image_url`）在 `_to_responses_content()` 中完成两种格式之间的转换。
- 遇到 429 限流时指数退避重试（最多 5 次）。
- `json_mode=True` 时启用结构化 JSON 输出，用于元数据提取和技术点提取。
- `prompts.py` 集中管理：通用风格约束（`GLOBAL_STYLE_PROMPT`）、通俗讲解角色提示词、info.json 的 JSON schema 提示词、图表分析风格约束。

### 3. HTML 导出（`src/html_exporter.py`）

- 自实现的 Markdown→HTML 渲染器（无外部依赖），支持目录生成、表格渲染、响应式 CSS。
- 渲染完成后，可选的 LLM 质量门控（`_llm_review_html`）会检查是否残留原始 Markdown 语法（如表格管道符），并自动修复。
- 输出结构：`html/index.html`（首页）+ `html/{paper_notes,eli5_notes,figs_notes}/index.html`。

### 配置（`src/config.py`）

从项目根目录的 `.env` 文件读取配置。关键环境变量：`model_provider`、`model`、`base_url`、`wire_api`（`responses` 或 `chat`）、`OPENAI_API_KEY`、`MINERU_API_TOKEN`。`MODEL_NAME_IMAGE` 未设置时默认使用 `MODEL_NAME_TEXT` 的值。当 `python-dotenv` 未安装时，回退到内置的 `.env` 解析器。

### 数据流

`main.py` 判断输入类型（PDF 还是目录），必要时执行 MinerU 流水线，找到源 `.md` 文件（优先级：`full.md` > `md.md` > 第一个 `.md`），构建包含文本 + base64 编码图片的 LLM 上下文（带 `cache_control` 标记以利用提示缓存），然后按顺序执行各分析步骤。输出写入 `outputs/<slug>/`，包含 `paper_notes.md`、`ELI5_notes.md`、`figs_notes.md`、`info.json` 和 `images/` 子目录。

## Vercel 部署

`vercel.json` 配置为将 `outputs/<paper>/html/` 作为静态站点部署。`buildCommand` 为 `null`——构建在本地完成，仅部署已生成的 HTML 文件。
