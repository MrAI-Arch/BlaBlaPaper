"""
HTML 导出模块 - 将生成的 Markdown 报告转换为可直接打开的静态 HTML。
"""
import html
import json
import os
import re
import shutil
from pathlib import Path

from . import logutil


REPORTS = [
    ("paper_notes.md", "paper_notes", "论文解析"),
    ("ELI5_notes.md", "eli5_notes", "通俗讲解"),
    ("figs_notes.md", "figs_notes", "图表详解"),
    ("translation_notes.md", "translation_notes", "原文翻译"),
]


def _llm_review_html(html_content, md_name):
    """
    LLM quality gate: check if generated HTML still contains raw Markdown syntax
    that wasn't rendered (e.g. table pipes | leaking through). If so, ask LLM to fix.
    Returns original content if anything fails.
    """
    try:
        from . import config
        from . import llm_client
    except (ImportError, ValueError):
        return html_content
    except Exception:
        return html_content

    # Quick check: look for raw table pipe patterns
    suspicious_patterns = ["|  ", "|---", "|\n|---"]
    found = any(p in html_content for p in suspicious_patterns)
    if not found:
        return html_content

    logutil.log(f"   -> LLM quality check: {md_name} ...", "INFO")

    escaped_html = html_content.replace("\\", "\\\\").replace("`", "\\`")
    prompt = (
        "Task: You are an HTML fixer. Below is an HTML fragment generated from Markdown.\n"
        "Check if it contains **unrendered raw Markdown syntax**, especially:\n"
        "\n"
        "1. **Table pipe leaks**: Look for raw `|` pipe characters that form Markdown table syntax\n"
        "   like `| header | value |` or `|---|---:|`. Convert these into proper <table> HTML.\n"
        "2. **Other raw Markdown**: Check for bare #, **, ---, ``` that weren't converted to HTML.\n"
        "3. **Tag issues**: Fix any unclosed or mis-nested HTML tags.\n"
        "\n"
        "Requirements:\n"
        "- Output ONLY the fixed HTML. No explanations, no markdown wrapping.\n"
        "- If nothing is wrong, output the original content unchanged.\n"
        "- Do NOT alter any text content, links, images, or CSS class names.\n"
        "\n"
        "HTML content:\n"
        "```\n"
        + escaped_html + "\n"
        "```"
    )

    try:
        result = llm_client.call_llm_with_cache(
            [{"role": "system", "content": "You are an HTML fixer. Output only the fixed HTML."}],
            prompt,
            config.API_KEY,
            config.API_URL,
            config.MODEL_NAME_TEXT,
            json_mode=False,
            stage_name=f"html_review.{md_name}"
        )
        if result and result.strip():
            cleaned = result.strip()
            if cleaned.startswith("```html"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
            if cleaned.startswith("<") and len(cleaned) > 50:
                logutil.log(f"    -> LLM quality check complete, issues fixed", "INFO")
                return cleaned
        return html_content
    except Exception as e:
        logutil.log(f"    -> LLM quality check skipped: {e}", "WARN")
        return html_content




def export_html_reports(output_dir, site_title="BlaBlaCutter", output_root=None):
    """
    为输出目录中的报告生成 HTML 页面。

    Args:
        output_dir: 报告输出目录，包含 paper_notes.md / ELI5_notes.md / figs_notes.md
        site_title: 页面左上角显示的站点标题
        output_root: 可选的静态站点根目录。传入 docs 时可直接用于 GitHub Pages。

    Returns:
        生成的 HTML 入口文件路径
    """
    output_path = Path(output_dir)
    collection_root = Path(output_root).expanduser() if output_root else None
    report_slug = _get_report_slug(output_path)
    html_root = collection_root / report_slug if collection_root else output_path / "html"
    html_root.mkdir(parents=True, exist_ok=True)
    if collection_root:
        collection_root.mkdir(parents=True, exist_ok=True)
        (collection_root / ".nojekyll").write_text("", encoding="utf-8")

    available_reports = [
        report for report in REPORTS
        if (output_path / report[0]).exists()
    ]
    if not available_reports:
        raise FileNotFoundError(f"未找到可导出的 Markdown 报告: {output_path}")

    _copy_static_assets(output_path, html_root)

    for md_name, route, label in available_reports:
        markdown_text = (output_path / md_name).read_text(encoding="utf-8")
        page_title = _extract_title(markdown_text) or label
        body_html, toc = _markdown_to_html(markdown_text, asset_prefix="../")
        body_html = _llm_review_html(body_html, md_name)
        page_dir = html_root / route
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "index.html").write_text(
            _render_page(
                title=page_title,
                site_title=site_title,
                body_html=body_html,
                toc=toc,
                nav_reports=available_reports,
                active_route=route,
                nav_prefix="../",
            ),
            encoding="utf-8",
        )

    index_html = _render_index(site_title, available_reports)
    index_path = html_root / "index.html"
    index_path.write_text(index_html, encoding="utf-8")

    if collection_root:
        collection_index = _render_collection_index(
            site_title,
            _collect_paper_sites(collection_root),
        )
        (collection_root / "index.html").write_text(collection_index, encoding="utf-8")

    return str(index_path)


def _get_report_slug(output_path):
    info_data = _read_info_json(output_path / "info.json")
    raw_slug = info_data.get("index") if info_data else None
    slug = _slugify(str(raw_slug or output_path.name))
    return slug or "report"


def _read_info_json(path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _copy_static_assets(output_path, html_root):
    """复制 GitHub Pages 静态站点需要的图片和元数据。"""
    images_source = output_path / "images"
    if images_source.is_dir():
        images_target = html_root / "images"
        images_target.mkdir(parents=True, exist_ok=True)
        for source in images_source.iterdir():
            target = images_target / source.name
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.is_file():
                shutil.copy2(source, target)

    info_source = output_path / "info.json"
    if info_source.exists():
        shutil.copy2(info_source, html_root / "info.json")

    (html_root / ".nojekyll").write_text("", encoding="utf-8")


def _collect_paper_sites(collection_root):
    papers = []
    for child in sorted(collection_root.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "index.html").exists():
            continue
        if not (child / "info.json").exists():
            continue

        info_data = _read_info_json(child / "info.json")
        title = info_data.get("paper_title") or child.name.replace("-", " ").title()
        description = info_data.get("description") or ""
        metadata = info_data.get("metadata") or {}
        meta_parts = []
        for key in ("venue", "year"):
            value = metadata.get(key)
            if value:
                meta_parts.append(str(value))

        papers.append({
            "slug": child.name,
            "title": title,
            "description": description,
            "meta": " · ".join(meta_parts),
        })
    return papers


def _extract_title(markdown_text):
    for line in markdown_text.splitlines():
        match = re.match(r"^#\s+(.+)$", line.strip())
        if match:
            return _strip_inline_markup(match.group(1))
    return None


def _markdown_to_html(markdown_text, asset_prefix=""):
    lines = markdown_text.splitlines()
    html_parts = []
    toc = []
    paragraph = []
    list_stack = []
    in_code = False
    code_lines = []
    table_lines = []
    in_table = False
    used_ids = {}

    def close_paragraph():
        if paragraph:
            text = " ".join(paragraph).strip()
            html_parts.append(f"<p>{_inline_markdown(text, asset_prefix)}</p>")
            paragraph.clear()

    def close_lists():
        while list_stack:
            _, tag = list_stack.pop()
            html_parts.append(f"</{tag}>")

    def close_table():
        nonlocal in_table
        if table_lines:
            html_parts.append(_render_table(table_lines, asset_prefix))
            table_lines.clear()
            in_table = False

    def unique_id(text):
        base = _slugify(text) or "section"
        count = used_ids.get(base, 0)
        used_ids[base] = count + 1
        return base if count == 0 else f"{base}-{count + 1}"

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                html_parts.append(
                    "<pre><code>"
                    + html.escape("\n".join(code_lines))
                    + "</code></pre>"
                )
                code_lines = []
                in_code = False
            else:
                close_paragraph()
                close_lists()
                close_table()
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        if not stripped:
            close_paragraph()
            close_lists()
            close_table()
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            close_paragraph()
            close_lists()
            close_table()
            level = len(heading.group(1))
            text = heading.group(2).strip()
            section_id = unique_id(_strip_inline_markup(text))
            if level <= 3:
                toc.append((level, _strip_inline_markup(text), section_id))
            html_parts.append(
                f'<h{level} id="{section_id}">'
                f'{_inline_markdown(text, asset_prefix)}'
                f'<a class="anchor" href="#{section_id}">#</a>'
                f"</h{level}>"
            )
            continue

        if re.fullmatch(r"[-*_]{3,}", stripped):
            close_paragraph()
            close_lists()
            close_table()
            html_parts.append("<hr>")
            continue

        list_item = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.+)$", line)
        if list_item:
            close_paragraph()
            close_table()
            indent = len(list_item.group(1).replace("\t", "    "))
            marker = list_item.group(2)
            tag = "ol" if marker.endswith(".") and marker[:-1].isdigit() else "ul"
            text = list_item.group(3).strip()

            while list_stack and indent < list_stack[-1][0]:
                _, old_tag = list_stack.pop()
                html_parts.append(f"</{old_tag}>")

            if list_stack and indent == list_stack[-1][0] and tag != list_stack[-1][1]:
                _, old_tag = list_stack.pop()
                html_parts.append(f"</{old_tag}>")

            if not list_stack or indent > list_stack[-1][0] or tag != list_stack[-1][1]:
                html_parts.append(f"<{tag}>")
                list_stack.append((indent, tag))

            html_parts.append(f"<li>{_inline_markdown(text, asset_prefix)}</li>")
            continue

        if re.fullmatch(r"!\[[^\]]*\]\([^)]+\)(\s+\*[^*]+\*)?", stripped):
            close_paragraph()
            close_lists()
            close_table()
            html_parts.append(f"<figure>{_inline_markdown(stripped, asset_prefix)}</figure>")
            continue

        # Detect markdown table row: starts with | and contains at least one |
        if stripped.startswith("|") and "|" in stripped[1:]:
            close_paragraph()
            close_lists()
            if not in_table:
                close_table()
            table_lines.append(stripped)
            in_table = True
            continue

        if in_table:
            close_table()

        close_lists()
        paragraph.append(stripped)

    close_table()
    close_paragraph()
    close_lists()
    if in_code:
        html_parts.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")

    return "\n".join(html_parts), toc


def _is_table_separator(stripped):
    """Check if a line is a markdown table separator (e.g. |---|---:|)"""
    s = stripped.strip()
    if not s.startswith("|") or not s.endswith("|"):
        return False
    cells = s.strip("|").split("|")
    for cell in cells:
        c = cell.strip()
        if not re.fullmatch(r":?-{3,}:?", c):
            return False
    return True


def _render_table(table_lines, asset_prefix):
    """Convert a block of markdown table lines into an HTML <table>."""
    if not table_lines:
        return ""

    header_row = None
    body_rows = []
    alignments = []
    has_header = True

    for raw_line in table_lines:
        stripped = raw_line.strip()
        if not stripped.startswith("|"):
            continue
        if _is_table_separator(stripped):
            cells = stripped.strip("|").split("|")
            for cell in cells:
                c = cell.strip()
                if c.startswith(":") and c.endswith(":"):
                    alignments.append("center")
                elif c.endswith(":"):
                    alignments.append("right")
                elif c.startswith(":"):
                    alignments.append("left")
                else:
                    alignments.append(None)
            continue
        if header_row is None:
            header_row = stripped
        else:
            if _is_table_separator(header_row):
                # First line was actually a separator -> rebuild alignments, no header
                alignments = []
                _cells = header_row.strip("|").split("|")
                for cell in _cells:
                    c = cell.strip()
                    if c.startswith(":") and c.endswith(":"):
                        alignments.append("center")
                    elif c.endswith(":"):
                        alignments.append("right")
                    elif c.startswith(":"):
                        alignments.append("left")
                    else:
                        alignments.append(None)
                has_header = False
                header_row = None
                body_rows.append(stripped)
            else:
                body_rows.append(stripped)

    if header_row is None and not body_rows:
        return ""

    if not alignments and body_rows:
        ncols = max(
            len(header_row.strip("|").split("|")) if header_row else 0,
            max(len(r.strip("|").split("|")) for r in body_rows) if body_rows else 0
        )
        alignments = [None] * ncols
    elif not alignments and header_row:
        ncols = len(header_row.strip("|").split("|"))
        alignments = [None] * ncols

    parts = ["<table>"]

    def parse_cells(cell_line):
        cells = cell_line.strip("|").split("|")
        return [c.strip() for c in cells]

    # Header
    if has_header and header_row:
        cells = parse_cells(header_row)
        parts.append("<thead><tr>")
        for i, cell in enumerate(cells):
            align = alignments[i] if i < len(alignments) and alignments[i] else None
            style = f' style="text-align:{align}"' if align else ""
            parts.append(f"<th{style}>{_inline_markdown(cell.strip(), asset_prefix)}</th>")
        parts.append("</tr></thead>")

    # Body
    if body_rows:
        parts.append("<tbody>")
        for row in body_rows:
            cells = parse_cells(row)
            parts.append("<tr>")
            for i, cell in enumerate(cells):
                align = alignments[i] if i < len(alignments) and alignments[i] else None
                style = f' style="text-align:{align}"' if align else ""
                parts.append(f"<td{style}>{_inline_markdown(cell.strip(), asset_prefix)}</td>")
            parts.append("</tr>")
        parts.append("</tbody>")

    parts.append("</table>")
    return "\n".join(parts)


def _inline_markdown(text, asset_prefix=""):
    placeholders = {}

    def stash(value):
        key = f"@@HTML{len(placeholders)}@@"
        placeholders[key] = value
        return key

    def image_repl(match):
        alt = html.escape(match.group(1), quote=True)
        src = html.escape(_rewrite_local_url(match.group(2), asset_prefix), quote=True)
        return stash(f'<img src="{src}" alt="{alt}" loading="lazy">')

    def link_repl(match):
        label = _inline_markdown(match.group(1), asset_prefix)
        href = html.escape(_rewrite_local_url(match.group(2), asset_prefix), quote=True)
        return stash(f'<a href="{href}">{label}</a>')

    def code_repl(match):
        return stash(f"<code>{html.escape(match.group(1))}</code>")

    text = re.sub(r"`([^`]+)`", code_repl, text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", image_repl, text)
    text = re.sub(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)", link_repl, text)

    escaped = html.escape(text)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)

    for key, value in placeholders.items():
        escaped = escaped.replace(key, value)
    return escaped


def _rewrite_local_url(url, asset_prefix):
    if re.match(r"^(https?:|data:|mailto:|#)", url):
        return url
    return asset_prefix + url.lstrip("./")


def _strip_inline_markup(text):
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_`#]+", "", text)
    return text.strip()


def _slugify(text):
    slug = re.sub(r"\s+", "-", text.strip().lower())
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff-]", "", slug)
    return slug.strip("-")


def _render_toc(toc):
    if not toc:
        return '<p class="muted">暂无目录</p>'

    links = []
    for level, text, section_id in toc:
        label = html.escape(text)
        links.append(
            f'<a class="toc-level-{level}" href="#{section_id}">{label}</a>'
        )
    return "\n".join(links)


def _render_nav(nav_reports, active_route, nav_prefix):
    links = []
    for _, route, label in nav_reports:
        active = " active" if route == active_route else ""
        href = f"{nav_prefix}{route}/index.html"
        links.append(f'<a class="nav-link{active}" href="{href}">{html.escape(label)}</a>')
    return "\n".join(links)


def _render_page(title, site_title, body_html, toc, nav_reports, active_route, nav_prefix):
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - {html.escape(site_title)}</title>
  <style>{_CSS}</style>
</head>
<body>
  <header class="topbar">
    <a class="brand" href="../index.html">{html.escape(site_title)}</a>
    <span class="page-title">{html.escape(title)}</span>
  </header>
  <div class="layout">
    <aside class="sidebar">
      <nav>
        <a class="nav-link" href="../index.html">Home</a>
        {_render_nav(nav_reports, active_route, nav_prefix)}
      </nav>
    </aside>
    <main class="content">
      <article>{body_html}</article>
    </main>
    <aside class="toc">
      <div class="toc-title">目录</div>
      {_render_toc(toc)}
    </aside>
  </div>
</body>
</html>
"""


def _render_index(site_title, nav_reports):
    links = "\n".join(
        f'<a class="report-card" href="{route}/index.html">'
        f'<span>{html.escape(label)}</span>'
        f'<small>{html.escape(md_name)}</small>'
        f"</a>"
        for md_name, route, label in nav_reports
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(site_title)}</title>
  <style>{_CSS}</style>
</head>
<body>
  <header class="topbar">
    <span class="brand">{html.escape(site_title)}</span>
    <span class="page-title">HTML Reports</span>
  </header>
  <main class="index">
    <h1>{html.escape(site_title)} 报告</h1>
    <div class="report-grid">{links}</div>
  </main>
</body>
</html>
	"""


def _render_collection_index(site_title, papers):
    if papers:
        links = "\n".join(
            '<a class="report-card paper-card" href="'
            + html.escape(paper["slug"], quote=True)
            + '/index.html">'
            + f'<span>{html.escape(paper["title"])}</span>'
            + (f'<small>{html.escape(paper["meta"])}</small>' if paper["meta"] else "")
            + (f'<p>{html.escape(paper["description"])}</p>' if paper["description"] else "")
            + "</a>"
            for paper in papers
        )
    else:
        links = '<p class="muted">暂无已发布论文</p>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(site_title)}</title>
  <style>{_CSS}</style>
</head>
<body>
  <header class="topbar">
    <span class="brand">{html.escape(site_title)}</span>
    <span class="page-title">Paper Library</span>
  </header>
  <main class="index">
    <h1>{html.escape(site_title)} 论文库</h1>
    <div class="report-grid">{links}</div>
  </main>
</body>
</html>
"""


_CSS = """
:root {
  color-scheme: light;
  --bg: #f7f8fb;
  --surface: #ffffff;
  --text: #1f2937;
  --muted: #64748b;
  --line: #e2e8f0;
  --accent: #2563eb;
  --accent-soft: #eff6ff;
  --sidebar: #0f172a;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Microsoft YaHei", sans-serif;
  line-height: 1.72;
}
.topbar {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 24px;
  height: 56px;
  padding: 0 28px;
  color: #fff;
  background: var(--sidebar);
  box-shadow: 0 1px 0 rgba(15, 23, 42, .12);
}
.brand {
  color: #fff;
  font-weight: 700;
  text-decoration: none;
  white-space: nowrap;
}
.page-title {
  overflow: hidden;
  color: #cbd5e1;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.layout {
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr) 260px;
  gap: 0;
  max-width: 1500px;
  margin: 0 auto;
}
.sidebar,
.toc {
  position: sticky;
  top: 56px;
  height: calc(100vh - 56px);
  overflow: auto;
  padding: 28px 18px;
}
.sidebar {
  border-right: 1px solid var(--line);
  background: #fff;
}
.toc {
  border-left: 1px solid var(--line);
  background: #fff;
}
.nav-link,
.toc a {
  display: block;
  color: var(--muted);
  text-decoration: none;
}
.nav-link {
  margin-bottom: 4px;
  padding: 9px 10px;
  border-radius: 6px;
  font-size: 14px;
}
.nav-link:hover,
.nav-link.active {
  color: var(--accent);
  background: var(--accent-soft);
}
.toc-title {
  margin-bottom: 10px;
  color: var(--text);
  font-size: 13px;
  font-weight: 700;
}
.toc a {
  padding: 5px 0;
  font-size: 13px;
}
.toc a:hover { color: var(--accent); }
.toc-level-3 { padding-left: 16px !important; }
.content {
  min-width: 0;
  padding: 36px 48px 80px;
}
article {
  max-width: 900px;
  margin: 0 auto;
  padding: 36px 44px 56px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}
h1, h2, h3, h4, h5, h6 {
  position: relative;
  line-height: 1.28;
  scroll-margin-top: 76px;
}
h1 {
  margin-top: 0;
  font-size: 30px;
}
h2 {
  margin-top: 42px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
  font-size: 24px;
}
h3 { margin-top: 30px; font-size: 20px; }
h4 { margin-top: 24px; font-size: 17px; }
.anchor {
  margin-left: 8px;
  color: #94a3b8;
  font-size: .72em;
  text-decoration: none;
  opacity: 0;
}
h1:hover .anchor,
h2:hover .anchor,
h3:hover .anchor,
h4:hover .anchor { opacity: 1; }
p { margin: 14px 0; }
ul, ol { padding-left: 1.35em; }
li { margin: 5px 0; }
a { color: var(--accent); }
hr {
  border: 0;
  border-top: 1px solid var(--line);
  margin: 28px 0;
}
code {
  padding: 2px 5px;
  border-radius: 4px;
  background: #f1f5f9;
  font-size: .92em;
}
pre {
  overflow: auto;
  padding: 16px;
  border-radius: 8px;
  background: #0f172a;
  color: #e2e8f0;
}
pre code { padding: 0; background: transparent; color: inherit; }
figure {
  margin: 24px 0;
  text-align: center;
}
figure img,
p img {
  max-width: 100%;
  height: auto;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
}
table {
  border-collapse: collapse;
  width: 100%;
  margin: 16px 0;
  font-size: 14px;
  overflow-x: auto;
  display: block;
}
table th, table td {
  padding: 8px 12px;
  border: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
  white-space: nowrap;
}
table thead th {
  background: #f1f5f9;
  font-weight: 700;
}
table tbody tr:nth-child(even) {
  background: #fafbfc;
}
table tbody tr:hover {
  background: var(--accent-soft);
}
figure em {
  display: block;
  margin-top: 8px;
  color: var(--muted);
  font-size: 14px;
}
.index {
  max-width: 900px;
  margin: 0 auto;
  padding: 56px 28px;
}
.report-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
}
.report-card {
  display: block;
  padding: 20px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
  color: var(--text);
  text-decoration: none;
}
.report-card:hover {
  border-color: #bfdbfe;
  box-shadow: 0 8px 24px rgba(15, 23, 42, .08);
}
.report-card span {
  display: block;
  font-weight: 700;
}
	.report-card small {
	  color: var(--muted);
	}
	.paper-card p {
	  margin: 10px 0 0;
	  color: var(--muted);
	  font-size: 14px;
	  line-height: 1.55;
	}
	.muted { color: var(--muted); }
@media (max-width: 1100px) {
  .layout { grid-template-columns: 190px minmax(0, 1fr); }
  .toc { display: none; }
}
@media (max-width: 760px) {
  .topbar { padding: 0 16px; }
  .layout { display: block; }
  .sidebar {
    position: static;
    height: auto;
    border-right: 0;
    border-bottom: 1px solid var(--line);
    padding: 12px;
  }
  .content { padding: 20px 12px 48px; }
  article { padding: 24px 18px 40px; }
  h1 { font-size: 25px; }
  h2 { font-size: 21px; }
}
"""
