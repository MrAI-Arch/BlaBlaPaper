"""TeX 源码 → MinerU 兼容工作目录的转换器（Pandoc 驱动）。

把含 .tex 的目录转成 full.md + images/ + tex_content_list.json，
形态与 MinerU 产物一致，下游 parser/core/utils/html_exporter 无需改动。

设计原则：防御式 + 失败即报错带定位（TexConvertError），不静默降级。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from . import logutil


class TexConvertError(Exception):
    """TeX 转换失败，携带阶段/文件/原因/附近内容/建议供定位。"""

    def __init__(self, stage, detail, file=None, snippet=None, hint=None):
        self.stage = stage
        self.detail = detail
        self.file = file
        self.snippet = snippet
        self.hint = hint
        super().__init__(self._format())

    def _format(self):
        lines = ["[TeX 转换失败] 阶段=%s" % self.stage]
        if self.file:
            lines.append("  文件: %s" % self.file)
        lines.append("  原因: %s" % self.detail)
        if self.snippet:
            snip = re.sub(r"\s+", " ", self.snippet.strip())
            if len(snip) > 200:
                snip = snip[:200] + "..."
            lines.append("  附近内容: %s" % snip)
        if self.hint:
            lines.append("  提示: %s" % self.hint)
        return "\n".join(lines)


def _log(msg, level="INFO"):
    logutil.log(msg, level)


def _warn(stage, msg):
    _log("[TeX 警告] 阶段=%s %s" % (stage, msg), "WARN")


# ---------- 检测 ----------

def _collect_tex_roots(path):
    roots = []
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return roots
    for name in names:
        if not name.lower().endswith(".tex"):
            continue
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(8192)
        except OSError:
            continue
        if re.search(r"\\documentclass", head):
            roots.append(full)
    return roots


def find_tex_root(path):
    """返回含 \\documentclass 的首个 .tex 文件路径，无则 None。"""
    roots = _collect_tex_roots(path)
    return roots[0] if roots else None


def is_tex_source(path):
    """目录是否为 TeX 源码（含 \\documentclass 的 .tex，且无现成 full.md/md.md）。"""
    if not os.path.isdir(path):
        return False
    if os.path.exists(os.path.join(path, "full.md")) or os.path.exists(os.path.join(path, "md.md")):
        return False
    return find_tex_root(path) is not None


# ---------- frontmatter 采集 ----------

def _balanced_brace(tex, cmd):
    """抓 \\cmd{...} 的花括号内容（平衡匹配），无则 None。"""
    m = re.search(r"\\" + re.escape(cmd) + r"\s*\{", tex)
    if not m:
        return None
    i = m.end() - 1  # 指向 '{'
    depth = 0
    for j in range(i, len(tex)):
        ch = tex[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return tex[i + 1:j]
    return None  # 花括号不闭合


def _strip_tex_format(s):
    """剥离标题/作者里常见的格式命令，保留纯文本。"""
    s = re.sub(r"(?<!\\)%[^\n]*", "", s)                         # 注释（非 \%）
    s = re.sub(r"\\xspace\b", "", s)                              # \xspace 不产生可见输出
    s = re.sub(r"\\vspace\s*\{[^}]*\}", " ", s)
    s = re.sub(r"\\vskip\s+\S+", " ", s)                          # \vskip 0.25in / \vskip -\parskip
    s = re.sub(r"\\hrule(?:(?:\s+(?:height|width|depth))\s+\S+)*", " ", s)
    s = re.sub(r"\\(smallskip|medskip|bigskip|par|parskip|noindent|centering|protect|relax)\b", " ", s)
    s = re.sub(r"\\(Large|LARGE|large|small|footnotesize|tiny|normalsize|bfseries|itshape|ttfamily|rmfamily|sffamily)\b", " ", s)
    # 展开字体命令 \textbf{X} -> X，循环处理一层嵌套
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\\(textbf|textit|emph|texttt|underline|textsc|textsf|textrm|textup|url)\s*\{([^{}]*)\}", r"\2", s)
    s = s.replace(r"\&", "&").replace(r"\%", "%").replace(r"\$", "$").replace(r"\_", "_")
    s = s.replace("\\\\", " ").replace(r"\,", " ").replace("~", " ")
    s = re.sub(r"\\[a-zA-Z]+\b", " ", s)                          # 残余裸命令
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _collect_simple_macros(tex_text):
    """收集无参 \\newcommand{\\name}{replacement}（跳过注释；replacement 可含嵌套花括号）。
    供标题/作者展开自定义宏（如 \\gmq -> \\textsc{GQA}），避免被当残余命令剥离成空。"""
    macros = {}
    text = re.sub(r"(?<!\\)%[^\n]*", "", tex_text)  # 去注释，避免采集到被注释的旧定义
    for m in re.finditer(r"\\newcommand\s*\{\s*\\([a-zA-Z]+)\s*\}\s*\{", text):
        name = m.group(1)
        i = m.end() - 1  # 指向 replacement 的 '{'
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    macros[name] = text[i + 1:j]
                    break
    return macros


def _expand_simple_macros(s, macros):
    """用宏定义替换 \\name（按键长降序避免短名前缀误替；迭代至稳定，限 5 轮防自引用死循环）。"""
    if not macros:
        return s
    for _ in range(5):
        prev = s
        for name in sorted(macros, key=len, reverse=True):
            s = re.sub(r"\\" + re.escape(name) + r"\b",
                       lambda _m, rep=macros[name]: rep, s)
        if s == prev:
            break
    return s


def _find_abstract(tex):
    m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", tex, re.S)
    return m.group(1) if m else None


def _harvest_frontmatter(tex_text, tex_root, input_dir, bib_path):
    """返回 (title, author, date, abstract_md)。title 取不到则报错。"""
    macros = _collect_simple_macros(tex_text)
    raw_title = _balanced_brace(tex_text, "title")
    if raw_title:
        raw_title = _expand_simple_macros(raw_title, macros)
    title = _strip_tex_format(raw_title) if raw_title else ""
    if not title:
        raise TexConvertError(
            "frontmatter", "\\title{} 解析结果为空",
            file=tex_root, snippet=raw_title or "(未找到 \\title)",
            hint="检查 \\title 是否含非标准格式；手动修正后重跑。",
        )

    raw_author = _balanced_brace(tex_text, "author")
    if raw_author:
        raw_author = _expand_simple_macros(raw_author, macros)
    author = _strip_tex_format(raw_author) if raw_author else ""
    if not author:
        _warn("frontmatter", "未解析到 \\author，作者信息将为空")

    raw_date = _balanced_brace(tex_text, "date")
    date = _strip_tex_format(raw_date) if raw_date else ""
    if date.lower() in ("today", r"\today"):
        date = ""

    abstract_raw = _find_abstract(tex_text)
    abstract_md = ""
    if abstract_raw:
        abstract_md = _pandoc_fragment_to_md(abstract_raw, input_dir)
        abstract_md = _strip_pandoc_html(abstract_md)
        abstract_md = _inline_footnotes(abstract_md)
        if not bib_path:
            abstract_md = _bare_citations_to_bracket(abstract_md)
        abstract_md = abstract_md.strip()
    else:
        _warn("frontmatter", "未找到 \\begin{abstract}，摘要将为空")
    return title, author, date, abstract_md


# ---------- pandoc ----------

def _run_pandoc(tex_text, cwd, bib_path):
    """对 tex_text 跑 pandoc latex->gfm；有 .bib 时启用 citeproc。返回 md。"""
    cmd = ["pandoc", "-f", "latex", "-t", "gfm", "--wrap=none"]
    if bib_path:
        cmd += ["--citeproc", "--bibliography", bib_path]
    try:
        r = subprocess.run(
            cmd, input=tex_text, cwd=cwd,
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        raise TexConvertError("pandoc", "pandoc 超时 (>300s)")
    except FileNotFoundError:
        raise TexConvertError("pandoc", "未找到 pandoc 可执行文件", hint="安装: sudo apt install pandoc")
    if r.returncode != 0:
        tail = "\n".join(r.stderr.strip().splitlines()[-8:])
        raise TexConvertError(
            "pandoc", "pandoc 非零退出 (code=%d)" % r.returncode,
            snippet=tail, hint="常见原因: 未识别的宏包/命令；查看 stderr 定位。",
        )
    if r.stderr.strip():
        for line in r.stderr.strip().splitlines():
            _warn("pandoc", line)
    return r.stdout


def _pandoc_fragment_to_md(fragment, cwd):
    """把一段 LaTeX 片段（如 abstract）转成 md。失败返回原文剥离版。"""
    try:
        r = subprocess.run(
            ["pandoc", "-f", "latex", "-t", "gfm", "--wrap=none"],
            input=fragment, cwd=cwd, capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return _strip_tex_format(fragment)
    if r.returncode != 0:
        _warn("pandoc_fragment", "pandoc stderr: %s" % r.stderr.strip()[:200])
        return _strip_tex_format(fragment)
    return r.stdout.strip()


def _inline_inputs(tex_text, base_dir, seen=None, depth=0):
    """递归把 \\input{f}/\\include{f} 替换为文件内容（带环保护与缺失告警）。"""
    if seen is None:
        seen = set()
    if depth > 20:
        return tex_text

    def repl(m):
        fname = m.group(1).strip()
        if not fname.endswith(".tex"):
            fname += ".tex"
        path = os.path.join(base_dir, fname)
        abs_path = os.path.abspath(path)
        if abs_path in seen:
            return ""
        if not os.path.exists(path):
            _warn("input", "找不到 \\input 文件: %s" % fname)
            return ""
        seen.add(abs_path)
        try:
            content = open(path, "r", encoding="utf-8", errors="replace").read()
        except OSError as e:
            _warn("input", "读取 %s 失败: %s" % (fname, e))
            return ""
        return _inline_inputs(content, base_dir, seen, depth + 1)

    return re.sub(r"\\(?:input|include)\s*\{([^}]+)\}", repl, tex_text)


def _strip_toggles(tex):
    """预处理 \\newtoggle / \\iftoggle（pandoc 不识别 etoolbox toggle）。
    \\newtoggle{name} 删除；\\iftoggle{name}{A}{B} 取 B（toggle 默认 false）。
    需平衡匹配花括号。"""
    tex = re.sub(r"\\newtoggle\s*\{[^}]*\}", "", tex)

    def repl_iftoggle(m, text, start):
        # 找到 {A}{B} 两段平衡花括号
        i = start
        branches = []
        for _ in range(2):
            while i < len(text) and text[i] != "{":
                i += 1
            if i >= len(text):
                return None, i
            depth = 0
            for j in range(i, len(text)):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        branches.append(text[i + 1:j])
                        i = j + 1
                        break
            else:
                return None, i
        # arxiv toggle 取 true 分支（第一个花括号块），其余取 false（第二个）
        toggle_name = m.group(1) if m else ""
        branch_idx = 0 if toggle_name == "arxiv" else 1
        return branches[branch_idx] if len(branches) == 2 else "", i

    out = []
    i = 0
    while i < len(tex):
        m = re.match(r"\\iftoggle\s*\{([^}]*)\}", tex[i:])
        if m:
            replacement, new_i = repl_iftoggle(m, tex, i + m.end())
            if replacement is not None:
                out.append(replacement)
                i = new_i
                continue
        out.append(tex[i])
        i += 1
    return "".join(out)


def _convert_raw_citations(tex):
    """无 .bib 时，把 \\citep{x}/\\cite{x} 转成 [x]，避免被 pandoc 丢弃。"""
    return re.sub(r"\\cite[a-zA-Z]*\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}", lambda m: "[" + m.group(1) + "]", tex)


def _bare_citations_to_bracket(md):
    """无 citeproc 时，把 pandoc 残留的 [@key] / [@key, p.5] 转成 [key]。"""
    return re.sub(r"\[@([^\]]+)\]", lambda m: "[" + m.group(1).split(",")[0].strip() + "]", md)


def _find_bib(input_dir):
    for name in sorted(os.listdir(input_dir)):
        if name.lower().endswith(".bib"):
            return os.path.join(input_dir, name)
    return None


# ---------- 后处理 ----------

def _strip_pandoc_html(md):
    """清掉 Pandoc gfm 输出里的裸 HTML（html_exporter 会 html.escape 它们成 &lt;...&gt;）。
    - 代码块(``` )内不动，避免破坏代码里的 < >。
    - 块级 <div id="refs" ...> → 替换为 "## References" 标题；其余 <div>/<span> 独占行整行删。
    - 行内 <a ...>X</a> → X（Pandoc 把 \\ref 交叉引用渲染成 <a>）；<br> → 空格。"""
    out = []
    in_code = False
    for line in md.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        m = re.match(r"^\s*<div\b([^>]*)>\s*$", line)
        if m:
            attrs = m.group(1)
            if re.search(r'id\s*=\s*"refs"', attrs) or re.search(r'class\s*=\s*"[^"]*references', attrs):
                out.append("## References")
            continue  # 其余 div 整行删
        if re.match(r"^\s*</?(div|span)\b[^>]*>\s*$", line):
            continue
        line = re.sub(r"</?a\b[^>]*>", "", line)
        line = re.sub(r"<br\s*/?>", " ", line)
        line = re.sub(r"</?span\b[^>]*>", "", line)
        out.append(line)
    return "\n".join(out)


def _inline_footnotes(md):
    """把 [^id]: 定义内联到正文 [^id] 处，丢弃定义块。原因: exporter 不识别脚注。"""
    defs = {}

    def grab(m):
        fid = m.group(1)
        text = re.sub(r"\s+", " ", m.group(2)).strip()
        defs[fid] = text
        return ""

    md = re.sub(
        r"^\[\^([^\]]+)\]:\s*(.*(?:\n(?:[ \t]+.*))*)",
        grab, md, flags=re.MULTILINE,
    )
    md = re.sub(r"\[\^([^\]]+)\]", lambda m: " (%s)" % defs.get(m.group(1), m.group(1)), md)
    return md


def _slugify(s):
    s = s.lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9\-]", "", s)
    return s.strip("-") or "fig"


def _rasterize(src_path, images_dir, stem):
    """把图片栅格化/复制到 images_dir/<stem>.*。成功返回文件名，失败 None。"""
    ext = os.path.splitext(src_path)[1].lower()
    os.makedirs(images_dir, exist_ok=True)
    try:
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"):
            dst = stem + ext
            shutil.copy2(src_path, os.path.join(images_dir, dst))
            return dst
        if ext == ".pdf":
            out_prefix = os.path.join(images_dir, stem)
            r = subprocess.run(
                ["pdftoppm", "-png", "-singlefile", "-r", "200", "-f", "1", "-l", "1",
                 src_path, out_prefix],
                capture_output=True, text=True, timeout=120,
            )
            dst = stem + ".png"
            if r.returncode != 0 or not os.path.exists(os.path.join(images_dir, dst)):
                _warn("rasterize", "pdftoppm 失败 %s: %s" % (os.path.basename(src_path), r.stderr.strip()[:200]))
                return None
            return dst
        if ext in (".eps", ".ps"):
            dst = stem + ".png"
            r = subprocess.run(
                ["gs", "-dEPSCrop", "-dSAFER", "-dBATCH", "-dNOPAUSE",
                 "-sDEVICE=png16m", "-r200",
                 "-sOutputFile=" + os.path.join(images_dir, dst), src_path],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0 or not os.path.exists(os.path.join(images_dir, dst)):
                _warn("rasterize", "gs 失败 %s: %s" % (os.path.basename(src_path), r.stderr.strip()[:200]))
                return None
            return dst
        _warn("rasterize", "不支持的图格式 %s（已跳过）" % ext)
        return None
    except subprocess.TimeoutExpired:
        _warn("rasterize", "栅格化超时 %s" % os.path.basename(src_path))
        return None


def _clean_html_inline(text):
    """剥离行内 HTML 标签（<span>/<strong> 等），保留文本与 math。"""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text).strip()


def _rasterize_image(src_abs, images_dir, seen_stems):
    """栅格化/复制一张图到 images/，返回文件名（失败 None）。stem 去重。"""
    stem = _slugify(os.path.splitext(os.path.basename(src_abs))[0])
    base, n = stem, 1
    while stem in seen_stems:
        n += 1
        stem = "%s-%d" % (base, n)
    seen_stems.add(stem)
    return _rasterize(src_abs, images_dir, stem)


def _rewrite_images(md, input_dir, images_dir):
    """栅格化图片到 images/ 并改写为 Markdown，收集 (filename, caption)。
    处理三种 Pandoc 产出：Markdown ![](path)、<figure><img/><figcaption/></figure>、独立 <img>。
    Pandoc 对多图/带 caption 的 figure 会输出成 HTML <img>（而非 Markdown ![]），
    早期只认 Markdown 正则会漏图（GQA 论文即此情况）。"""
    entries = []
    seen_stems = set()

    # 1) Markdown ![cap](path)
    def md_repl(m):
        cap, path = m.group(1), m.group(2).strip()
        if path.startswith(("http://", "https://")):
            return m.group(0)
        src_abs = os.path.join(input_dir, path)
        if not os.path.exists(src_abs):
            _warn("image", "找不到图片文件: %s（引用已移除）" % path)
            return ""
        fname = _rasterize_image(src_abs, images_dir, seen_stems)
        if not fname:
            return ""
        entries.append((fname, cap))
        return "![%s](images/%s)" % (cap, fname)
    md = re.sub(r"!\[(.*?)\]\((.*?)\)", md_repl, md)

    # 2) <figure>...</figure> 块（Pandoc 把多图/带 caption 的 figure 输出成 HTML）
    def fig_repl(m):
        block = m.group(0)
        cap_m = re.search(r"<figcaption[^>]*>(.*?)</figcaption>", block, re.S)
        cap = _clean_html_inline(cap_m.group(1)) if cap_m else ""
        img_tags = re.findall(r"<(?:img|embed|object)\b[^>]*>", block)
        if not img_tags:
            return cap  # 无 <img>（多为 TikZ 被 pandoc 丢弃）：保留 caption 文本作上下文
        out = []
        for tag in img_tags:
            src_m = re.search(r'\b(?:src|data)=["\']([^"\']+)["\']', tag)
            if not src_m or src_m.group(1).startswith(("http://", "https://")):
                continue
            src_abs = os.path.join(input_dir, src_m.group(1))
            if not os.path.exists(src_abs):
                _warn("image", "找不到图片文件: %s（引用已移除）" % src_m.group(1))
                continue
            alt_m = re.search(r'\balt=["\']([^"\']*)["\']', tag)
            alt = alt_m.group(1) if alt_m else ""
            fname = _rasterize_image(src_abs, images_dir, seen_stems)
            if not fname:
                continue
            entries.append((fname, cap or alt))
            out.append("![%s](images/%s)" % (cap or alt, fname))
        return "\n\n".join(out)
    md = re.sub(r"<figure\b[^>]*>.*?</figure>", fig_repl, md, flags=re.S)

    # 3) 残余独立 <img>/<embed>/<object ...>（不在 <figure> 里，如 Pandoc 包在 <div> 里的单图；<embed> 多见于 .pdf includegraphics）
    def img_repl(m):
        tag = m.group(0)
        src_m = re.search(r'\b(?:src|data)=["\']([^"\']+)["\']', tag)
        if not src_m or src_m.group(1).startswith(("http://", "https://")):
            return ""
        src_abs = os.path.join(input_dir, src_m.group(1))
        if not os.path.exists(src_abs):
            _warn("image", "找不到图片文件: %s（引用已移除）" % src_m.group(1))
            return ""
        alt_m = re.search(r'\balt=["\']([^"\']*)["\']', tag)
        alt = alt_m.group(1) if alt_m else ""
        fname = _rasterize_image(src_abs, images_dir, seen_stems)
        if not fname:
            return ""
        entries.append((fname, alt))
        return "![%s](images/%s)" % (alt, fname)
    md = re.sub(r"<(?:img|embed|object)\b[^>]*>", img_repl, md)

    # 4) 清理残余 <figure>/<figcaption> 标签（不匹配块的残片）
    md = re.sub(r"</?(?:figure|figcaption)\b[^>]*>", "", md)

    return md, entries


# ---------- TikZ ----------

def _detect_tikz(input_dir):
    pattern = re.compile(r"\\begin\{tikzpicture\}|\\tikz\b|\\begin\{axis\}|\\pgfplots")
    hits = []
    for root, _, files in os.walk(input_dir):
        for name in files:
            if not name.lower().endswith(".tex"):
                continue
            path = os.path.join(root, name)
            try:
                content = open(path, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            n = len(pattern.findall(content))
            if n:
                hits.append((path, n))
    return hits


# ---------- 校验与摘要 ----------

def _validate_and_summarize(full_md, tex_root, title, author, abstract_md, figure_entries, bib_path, tikz_hits):
    n_sections = len(re.findall(r"^#+\s", full_md, re.MULTILINE))
    if n_sections == 0:
        raise TexConvertError(
            "validate", "转换后未检出任何章节标题（pandoc 可能未正常解析）",
            file=tex_root, hint="查看上方 [TeX 警告]；确认 tex 根文件正确。",
        )
    n_code = len(re.findall(r"^```", full_md, re.MULTILINE)) // 2
    n_table_rows = len(re.findall(r"^\|.*\|\s*$", full_md, re.MULTILINE))
    cite_desc = "citeproc 已解析" if bib_path else "裸引用兜底"
    tikz_desc = "检测到 %d 处" % sum(c for _, c in tikz_hits) if tikz_hits else "未检测到"
    _log("[TeX 转换摘要] 根文件=%s" % os.path.basename(tex_root))
    _log("  标题=%s  作者=%s" % (title, author or "(空)"))
    _log("  摘要=%s  章节=%d  代码块≈%d  表格行≈%d" % (
        "✓" if abstract_md else "空", n_sections, n_code, n_table_rows))
    _log("  图片=%d  引用=%s  TikZ=%s" % (len(figure_entries), cite_desc, tikz_desc))


# ---------- 主入口 ----------

def convert(input_dir, assume_yes=False):
    """把 TeX 源码目录转成 MinerU 兼容的临时工作目录，返回其路径。"""
    input_dir = os.path.abspath(input_dir)
    roots = _collect_tex_roots(input_dir)
    if not roots:
        raise TexConvertError(
            "find_root", "未找到含 \\documentclass 的 .tex 文件",
            file=input_dir, hint="确认目录是 LaTeX 源码（含 .tex 且有 \\documentclass）。",
        )
    tex_root = roots[0]
    if len(roots) > 1:
        _warn("find_root", "多个 .tex 含 \\documentclass，使用: %s" % os.path.basename(tex_root))

    # 1. TikZ 检测与交互
    tikz_hits = _detect_tikz(input_dir)
    if tikz_hits:
        _warn("tikz", "检测到 TikZ 图: %s" % ", ".join(
            "%s(%d处)" % (os.path.relpath(f, input_dir), c) for f, c in tikz_hits))
        _log("  本机无 TeX 发行版，TikZ 图无法渲染，将跳过（不出现在图表报告），正文相关位置可能残留 LaTeX 源码。")
        if not assume_yes and sys.stdin.isatty():
            ans = input("  是否继续？[y/N] ").strip().lower()
            if ans != "y":
                raise TexConvertError("tikz", "用户取消（检测到 TikZ 图且未确认继续）")

    # 2. 读根 tex + 递归 inline \input
    try:
        tex_text = open(tex_root, "r", encoding="utf-8", errors="replace").read()
    except OSError as e:
        raise TexConvertError("read", "读取根文件失败: %s" % e, file=tex_root)
    tex_text = _inline_inputs(tex_text, input_dir)

    # 2b. 预处理 toggle（pandoc 不识别 \iftoggle）
    tex_text = _strip_toggles(tex_text)

    # 3. .bib
    bib_path = _find_bib(input_dir)

    # 4. frontmatter
    title, author, date, abstract_md = _harvest_frontmatter(tex_text, tex_root, input_dir, bib_path)

    # 5. pandoc body（无 .bib 时预处理裸引用，避免被丢弃）
    body_tex = tex_text if bib_path else _convert_raw_citations(tex_text)
    body_md = _run_pandoc(body_tex, input_dir, bib_path)
    if not body_md.strip():
        raise TexConvertError(
            "pandoc", "pandoc 输出为空", file=tex_root,
            hint="tex 可能含 pandoc 不支持的宏包；查看 [TeX 警告] 定位。",
        )

    # 6. 临时 work_dir
    keep = os.getenv("BLABLA_KEEP_TEX_BUILD", "").lower() in ("1", "true", "yes")
    work_dir = tempfile.mkdtemp(prefix="blabla_tex_")
    images_dir = os.path.join(work_dir, "images")

    try:
        # 7. 后处理
        body_md = _strip_pandoc_html(body_md)
        body_md = _inline_footnotes(body_md)
        body_md, figure_entries = _rewrite_images(body_md, input_dir, images_dir)
        if not bib_path:
            body_md = _bare_citations_to_bracket(body_md)
            bbl_refs = _bbl_to_text(input_dir)
            if bbl_refs:
                body_md += "\n\n## References\n\n" + bbl_refs

        # 8. 组装 full.md
        header = "# %s\n\n" % title
        if author:
            header += "*%s*\n\n" % author
        if abstract_md:
            header += "## Abstract\n\n%s\n\n---\n\n" % abstract_md
        full_md = header + body_md.strip() + "\n"
        with open(os.path.join(work_dir, "full.md"), "w", encoding="utf-8") as f:
            f.write(full_md)

        # 9. 合成 tex_content_list.json
        content_list = [{"type": "text", "text": title}]
        for fname, cap in figure_entries:
            content_list.append({
                "type": "image",
                "img_path": "images/%s" % fname,
                "image_caption": [cap] if cap else [],
            })
        with open(os.path.join(work_dir, "tex_content_list.json"), "w", encoding="utf-8") as f:
            json.dump(content_list, f, ensure_ascii=False, indent=2)

        # 10. _tex_meta.json（供 build_meta_context 使用）
        with open(os.path.join(work_dir, "_tex_meta.json"), "w", encoding="utf-8") as f:
            json.dump({"title": title, "author": author, "date": date}, f, ensure_ascii=False, indent=2)

        # 11. 校验 + 摘要
        _validate_and_summarize(full_md, tex_root, title, author, abstract_md, figure_entries, bib_path, tikz_hits)

        if keep:
            _log("[TeX] 保留临时工作目录（BLABLA_KEEP_TEX_BUILD=1）: %s" % work_dir)
        return work_dir
    except Exception:
        if not keep:
            shutil.rmtree(work_dir, ignore_errors=True)
        raise


def _bbl_to_text(input_dir):
    """从 .bbl 抽取参考文献条目为纯文本列表（无 .bib 时的兜底）。"""
    bbl_path = None
    for name in sorted(os.listdir(input_dir)):
        if name.lower().endswith(".bbl"):
            bbl_path = os.path.join(input_dir, name)
            break
    if not bbl_path:
        return ""
    try:
        bbl = open(bbl_path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    entries = re.findall(
        r"\\bibitem(?:\[[^\]]*\])?\s*\{[^}]*\}(.*?)(?=\\bibitem|\\end\{thebibliography\})",
        bbl, re.S,
    )
    out = []
    for e in entries:
        e = re.sub(r"\\newblock\s*", " ", e)
        e = _strip_tex_format(e)
        if e:
            out.append("- " + e)
    return "\n".join(out)


def build_meta_context(work_dir):
    """从 _tex_meta.json 合成补充信息（替代 get_pdf_metadata_context）。"""
    meta_path = os.path.join(work_dir, "_tex_meta.json")
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    parts = []
    if meta.get("author"):
        parts.append("作者: %s" % meta["author"])
    if meta.get("date"):
        parts.append("日期: %s" % meta["date"])
    if not parts:
        return None
    return (
        "=== 补充信息（来自 TeX 源码） ===\n\n"
        + "\n".join(parts) + "\n\n"
        "请参考上述补充信息来提取论文的基本信息（作者、发表年份等）。"
    )
