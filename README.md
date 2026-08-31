# BlaBlaPaper

> **让阅读英文变简单，让阅读论文变简单，让每个人都能通俗地看懂所有科研。**

BlaBlaPaper 是一个自动化论文解读工具。输入一篇英文 PDF，它会自动提取图表，并生成技术解析、通俗讲解、图表详解、原文翻译四份报告，帮你突破语言和术语的障碍，直接抓住论文核心。

---

## 为什么做 BlaBlaPaper？

读论文常常是三座大山：

- **英文阅读负担** —— 不是看不懂，而是看得慢、容易累
- **术语门槛** —— 堆砌的概念让人抓不住重点
- **信息过载** —— 一篇论文几页到十几页，看完脑袋空空

BlaBlaPaper 尝试做一件事：**把论文讲成人话**。让技术归技术，让语言不再成为障碍。

---

## 核心特性

| 特性 | 说明 |
|---|---:|
| **🤖 PDF 一键解析** | 基于 MinerU 服务，自动提取文本、图片、表格 |
| **📊 四层报告** | 技术深挖 + 通俗讲解（ELI5）+ 图表详解 + 原文翻译 |
| **🧠 LLM 驱动** | 支持 OpenAI / DeepSeek 等多种 API，智能提取核心贡献 |
| **🖼️ 图表自动分析** | 每张图单独解读，不需要反复翻原文找说明 |
| **📝 元数据提取** | 作者、期刊、年份等基本信息自动整理 |
| **🌐 HTML 导出** | 生成可直接打开的静态网页，分享方便 |
| **🔌 开源免费** | 模块化设计，按自己的需求修改和扩展 |

---

## 快速开始

### 环境要求

- Python 3.10+
- 一个 LLM API Key（OpenAI / DeepSeek 等）
- （可选）MinerU Token，用于 PDF 解析
- （可选）[pandoc](https://pandoc.org/)，用于 TeX 源码输入模式（`sudo apt install pandoc`）

### 安装

```bash
git clone https://github.com/MrAI-Arch/BlaBlaPaper.git
cd BlaBlaPaper
pip install -r requirements.txt
```

### 配置

在项目根目录创建 `.env` 文件：

```ini
# ---- OpenAI（推荐） ----
model_provider=OpenAI
model=gpt-5.5
base_url=https://api.openai.com/v1
wire_api=responses
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# ---- DeepSeek（二选一即可） ----
# model_provider=DeepSeek
# model=deepseek-v4-flash
# base_url=https://api.deepseek.com
# wire_api=chat
# OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# ---- PDF 解析（可选，没有则跳过 PDF 输入模式） ----
MINERU_API_TOKEN=your_mineru_token_here
```

> **DeepSeek 支持**：`wire_api` 设为 `chat`，`base_url` 设为 `https://api.deepseek.com/v1`，填入你的 DeepSeek API Key 即可。

### 运行

```bash
# ✨ 分析一篇 PDF（自动解析 + 生成报告）
python main.py /path/to/paper.pdf

# 生成报告的同时导出 HTML 网页
python main.py /path/to/paper.pdf --html

# 直接分析论文的 TeX 源码目录（代码/公式/表格更精确，需 pandoc）
python main.py /path/to/arXiv-xxxx-source/

# 追加/更新到 GitHub Pages 论文库 docs/
python main.py /path/to/paper.pdf --pages-dir docs

# 已有 Markdown 报告，只导出 HTML
python main.py /path/to/outputs/paper-title --html-only

# 已有 Markdown 报告，追加/更新到 GitHub Pages 论文库
python main.py /path/to/outputs/paper-title --html-only --pages-dir docs
```

> **输入自动识别**：传入 PDF → 走 MinerU 解析；传入含 `.tex`（带 `\documentclass`）的目录 → 走 TeX 源码模式（pandoc 转换，代码/公式/表格逐字精确，自动栅格化图片，需 pandoc + poppler/ghostscript）；传入含 `full.md` 的目录 → 按 MinerU 产物处理。TeX 模式若检测到 TikZ 图会提示确认（`--yes` 可跳过），转换失败会带阶段与定位信息报错。

> **日志**：运行日志同时打印到命令行并写入 `outputs/<paper>/run-<时间戳>.log`（保留历史）。默认只显示阶段进度；加 `-v` / `--verbose`（或设 `BLABLA_VERBOSE=1`）显示每次 LLM 调用的完整信息、checkpoint/parallel 等调试细节；LLM 出错时诊断信息始终打印。

输出在 `outputs/` 目录下：

```
outputs/your-paper-title/
├── paper_notes.md        # 技术解析报告
├── ELI5_notes.md         # 通俗讲解报告
├── figs_notes.md         # 图表详解报告
├── translation_notes.md  # 原文翻译报告
├── info.json             # 论文元数据
├── run-*.log             # 运行日志（带时间戳，保留历史）
└── html/                 # 静态网页（--html 或 --html-only）
    ├── index.html
    ├── paper_notes/
    ├── eli5_notes/
    ├── figs_notes/
    ├── translation_notes/
    └── images/
```

双击 `html/index.html` 即可在浏览器中查看。

### 发布到 GitHub Pages

`--pages-dir docs` 会把每篇论文发布到独立子目录，并自动生成 `docs/index.html` 论文选择页。最简单的发布方式是把生成好的 `docs/` push 到 GitHub：

```bash
python main.py /path/to/paper.pdf --pages-dir docs
git add docs
git commit -m "publish paper report"
git push
```

第一次使用时，在 GitHub 仓库里进入 `Settings -> Pages`，选择 `Deploy from a branch`，Branch 选 `main`，Folder 选 `/docs`。之后每次更新 `docs/` 并 push，GitHub Pages 会自动发布静态页面。

生成后的 `docs/` 结构如下：

```
docs/
├── .nojekyll
├── index.html                    # 论文选择页
├── paper-title-a/
│   ├── index.html
│   ├── info.json
│   ├── images/
│   ├── paper_notes/
│   ├── eli5_notes/
│   ├── figs_notes/
│   └── translation_notes/
└── paper-title-b/
    └── ...
```

如果已经有 `outputs/your-paper-title/`，不需要重新跑 LLM，直接执行：

```bash
python main.py outputs/your-paper-title --html-only --pages-dir docs
```

---

## 项目结构

```
BlaBlaPaper/
├── main.py                  # 主入口
├── src/
│   ├── config.py            # 配置加载（.env / 环境变量）
│   ├── mineru_client.py     # MinerU PDF 解析客户端
│   ├── parser.py            # Markdown 解析、元数据提取
│   ├── llm_client.py        # LLM API 调用（支持 Responses API 和 Chat）
│   ├── core.py              # 核心业务逻辑
│   ├── prompts.py           # 提示词模板
│   ├── tex_loader.py        # TeX 源码 → Markdown 转换（pandoc 驱动）
│   ├── html_exporter.py     # Markdown → HTML 导出
│   └── utils.py             # 工具函数
├── outputs/                 # 报告输出
├── .env                     # API 配置（不上传）
├── .gitignore
├── requirements.txt
└── README.md
```

---

## 支持的大模型

BlaBlaPaper 兼容 **OpenAI-compatible API**，理论上可以接入任何兼容服务：

| 服务商 | 配置示例 |
|---|---|
| **OpenAI** | `base_url=https://api.openai.com/v1` |
| **DeepSeek** | `base_url=https://api.deepseek.com/v1` |
| 阿里云百炼 | `base_url=https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 硅基流动 | `base_url=https://api.siliconflow.cn/v1` |
| 本地 Ollama | `base_url=http://localhost:11434/v1` |

只需在 `.env` 中修改 `base_url`、`model`、`wire_api` 即可切换。

---

## 输出示例

<details>
<summary>点击展开</summary>

### 技术报告（paper_notes.md）

结构化分析：背景、动机、核心技术、实验方法、结果解读。

### 通俗讲解（ELI5_notes.md）

用直白的语言和类比解释论文的核心创新——就像你的导师在白板上画了几笔。

### 图表详解（figs_notes.md）

逐图分析，结合原文上下文说明每张图/表的含义和关键发现。

### 原文翻译（translation_notes.md）

按原文结构逐段翻译为简体中文，**保留图片、公式、引用编号、标题层级、表格和代码**——相当于一份中文排版的论文全文，方便对照阅读。

### HTML 网页

四份报告自动生成带导航栏和目录的静态网页，支持随时在浏览器中打开查看。

</details>

---

## 常见问题

**Q: 提示 `OPENAI_API_KEY not found`？**
A: 检查 `.env` 文件是否在项目根目录，且字段名拼写正确。

**Q: 怎么只重新生成 HTML 不改报告？**
A: 用 `--html-only` 模式：`python main.py /path/to/outputs/paper-title --html-only`

**Q: 原文翻译和通俗讲解有什么区别？**
A: 原文翻译（translation_notes.md）按原文结构逐段直译，保留图片/公式/引用/表格/代码，相当于中文版全文；通俗讲解（ELI5_notes.md）只提炼核心创新点，用大白话和类比重新讲一遍，不复述全文。

**Q: MinerU Token 怎么获取？**
A: 访问 [MinerU Token 管理](https://mineru.net/apiManage/token) 创建。

**Q: 什么时候用 TeX 源码模式？**
A: 当你能拿到论文的 LaTeX 源码（如 arXiv 的 source tarball）时优先用 TeX 模式——代码、公式、表格的精度远高于 PDF→MinerU 路径（MinerU 会把代码逐字符 OCR 拼错、把多列表格拼坏）。安装 pandoc 后传入解压后的源码目录即可，工具会自动识别。检测到 TikZ 图会提示（本工具不渲染 TikZ，仅跳过并警告）。

---

## 后续计划

- **跨 notes 并发、notes 内串行**：当前流水线是"组内并发、组间串行"（深挖组 → ELI5 组 → 翻译组 → 图组接力，每组内部点间并发）。计划反过来——让 `paper_notes` / `ELI5_notes` / `translation_notes` 三条独立轨道并行，单条 notes 内部顺序生成（为内容连贯性接受串行）。`translation` 不依赖技术点提取，可最早启动。目的：在保证单份报告连贯的前提下用跨 notes 并发换吞吐。
- **figs_notes 按需生成**：图片多时逐图分析是总执行时延的大头。计划支持按需 / 限流 / 懒生成 `figs_notes`（或设为可选），避免无谓分析大量图片拖慢流水线。

---

## License

MIT

---

**BlaBlaPaper —— 让阅读英文变简单，让阅读论文变简单，让每个人都能通俗地看懂所有科研。**
