"""
核心业务逻辑模块 - 组装各模块实现报告生成流程
"""
import json
import os
import copy
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from . import prompts
from . import llm_client
from . import logutil
from . import utils
from . import parser


def _safe_name(value):
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value).strip().lower())
    return value.strip("-") or "checkpoint"


def _safe_pbar_update(pbar, n=1):
    """Update progress bar; silently ignore broken-pipe errors from closed terminals."""
    if pbar is None:
        return
    try:
        pbar.update(n)
    except (BrokenPipeError, OSError, IOError):
        pass


def _checkpoint_path(checkpoint_dir, name, suffix):
    if not checkpoint_dir:
        return None
    os.makedirs(checkpoint_dir, exist_ok=True)
    return os.path.join(checkpoint_dir, f"{_safe_name(name)}{suffix}")


def _load_text_checkpoint(checkpoint_dir, name):
    path = _checkpoint_path(checkpoint_dir, name, ".md")
    if path and os.path.exists(path):
        logutil.log(f"[checkpoint] reuse {os.path.basename(path)}", "DEBUG")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return None


def _save_text_checkpoint(checkpoint_dir, name, value):
    path = _checkpoint_path(checkpoint_dir, name, ".md")
    if path and value is not None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
    return value


def _load_json_checkpoint(checkpoint_dir, name):
    path = _checkpoint_path(checkpoint_dir, name, ".json")
    if path and os.path.exists(path):
        logutil.log(f"[checkpoint] reuse {os.path.basename(path)}", "DEBUG")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_json_checkpoint(checkpoint_dir, name, value):
    path = _checkpoint_path(checkpoint_dir, name, ".json")
    if path and value is not None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
    return value


def _run_ordered_parallel(label, items, worker, executor=None, pbar=None):
    if not items:
        return []
    own_executor = executor is None
    if own_executor:
        max_workers = min(config.TEXT_MAX_WORKERS, len(items))
        if max_workers <= 1:
            results = []
            for item in items:
                results.append(worker(item))
                _safe_pbar_update(pbar)
            return results
        executor = ThreadPoolExecutor(max_workers=max_workers)
        logutil.log(f"[parallel] {label}: workers={max_workers} tasks={len(items)}", "DEBUG")
    else:
        logutil.log(f"[parallel] {label}: shared-executor tasks={len(items)}", "DEBUG")

    results = [None] * len(items)
    future_to_idx = {
        executor.submit(worker, item): idx
        for idx, item in enumerate(items)
    }
    try:
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                logutil.log(f"[parallel] {label}: task={idx + 1} failed: {e}", "ERROR", stage=label)
                results[idx] = None
            _safe_pbar_update(pbar)
    finally:
        if own_executor:
            executor.shutdown(wait=True)
    return results


def build_full_context(text_content, image_paths):
    """
    构建完整的 LLM 上下文消息（包含文本和图片）

    Args:
        text_content: 论文文本内容
        image_paths: 图片文件路径列表

    Returns:
        消息列表，用于 LLM API 调用
    """
    # [Cache Optimization] 统一 System Prompt
    messages = [{"role": "system", "content": config.UNIFIED_SYSTEM_PROMPT}]

    # [Cache Optimization] Marker 1: Text Base
    user_content = [
        {
            "type": "text",
            "text": utils.format_document_content(text_content),
            "cache_control": {"type": "ephemeral"}
        }
    ]

    seen = set()
    for p in image_paths:
        if p in seen:
            continue
        seen.add(p)
        try:
            b64 = utils.encode_image_to_base64(p)
            user_content.append({"type": "text", "text": f"图片文件名: {os.path.basename(p)}"})
            user_content.append({
                "type": "image_url",
                "image_url": {"url": b64, "detail": "auto"}
            })
        except Exception:
            pass

    # [Cache Optimization] Marker 2: Full Context
    if len(user_content) > 1:
        if "cache_control" not in user_content[-1]:
            user_content[-1]["cache_control"] = {"type": "ephemeral"}

    messages.append({"role": "user", "content": user_content})
    messages.append({"role": "assistant", "content": "我已阅读文档和图片。"})
    return messages


def extract_tech_points(context_messages, model_name, checkpoint_dir=None):
    """
    提取核心技术点列表
    [Structured Output] 使用 JSON 模式以确保解析稳定。
    """
    cached = _load_json_checkpoint(checkpoint_dir, "tech_points")
    if cached is not None:
        return cached

    logutil.log(f"\n--- [核心技术提取] 正在提取关键技术点列表 (Model: {model_name}) ---", "INFO", stage="tech_points")

    # [Structured Output] 提示词中必须包含 'JSON' 关键词
    prompt = """
    任务：提炼本文**真正独立**的核心技术点。

    **数量由论文创新密度决定**：可能只有 1-2 个，也可能 5-6 个。宁可少而精，绝不凑数。

    **反换角度原则（最重要）**：
    若若干个点只是同一机制的不同侧面（如"机制本身" / "该机制解决的问题" / "该机制的实现" / "该机制的理论分析"），**必须合并为一个点**。
    每个点都要能在不重复其他点的前提下独立成立。

    **Output Restriction**:
    The output **MUST** be a valid JSON object. Do not include any markdown formatting or explanatory text outside the JSON.

    JSON Format:
    {
        "points": [
            {
                "name": "技术点名称 (如: Multi-Query Attention)",
                "scope": "本点的内容边界：专讲什么、不涉及什么（不涉及的部分归属其他点）。各点 scope 必须互不重叠。",
                "context": "原文关键描述 (1句话)"
            }
        ]
    }

    要求：
    1. name：简洁的技术点名称。
    2. scope：明确划出本点的内容地盘——讲什么、不讲什么；各点 scope 之间不得重叠。
    3. context：原文中支撑该点的一句关键描述。

    请严格按照上述 JSON 格式提取。
    """

    json_str = llm_client.call_llm_with_cache(
        context_messages,
        prompt,
        config.API_KEY,
        config.API_URL,
        model_name,
        json_mode=True,
        stage_name="tech_points"
    )

    if not json_str:
        return []

    try:
        clean = json_str.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean)
        points = data.get("points", [])
        return _save_json_checkpoint(checkpoint_dir, "tech_points", points)
    except json.JSONDecodeError as e:
        logutil.log(f"[解析错误] JSON 解析失败: {e}\n原始内容: {json_str}", "ERROR")
        return []
    except Exception as e:
        logutil.log(f"[未知错误] {e}", "ERROR")
        return []

def analyze_bibliographic_info(info_data):
    """
    从 info_data 中提取并组织论文基本信息

    Args:
        info_data: 包含 metadata 字段的字典

    Returns:
        格式化的论文基本信息文本
    """
    logutil.log("\n--- 正在提取: 论文基本信息 ---", "INFO")

    if not info_data or "metadata" not in info_data:
        return "未能提取到论文基本信息。"

    metadata = info_data.get("metadata", {})
    parts = []

    # 1. 作者信息
    authors = metadata.get("authors", [])
    if authors:
        authors_str = ", ".join(authors) if isinstance(authors, list) else str(authors)
        parts.append(f"**作者 (Authors)**: {authors_str}")

    # 2. 发表期刊/会议
    venue = metadata.get("venue", "")
    if venue:
        parts.append(f"**发表期刊/会议 (Journal/Conference)**: {venue}")

    # 3. 发表年份
    year = metadata.get("year", "")
    if year:
        parts.append(f"**发表年份 (Publication Year)**: {year}")

    # 4. 机构信息（如果有）
    affiliations = metadata.get("affiliations", [])
    if affiliations:
        affil_str = ", ".join(affiliations) if isinstance(affiliations, list) else str(affiliations)
        parts.append(f"**研究机构 (Affiliations)**: {affil_str}")

    return "\n\n".join(parts) if parts else "未能提取到论文基本信息。"


def generate_tech_deep_dive(context_messages, innovation_data, valid_filenames, model_name, caption_map, checkpoint_dir=None, executor=None, pbar=None):
    """
    基于已提取的点生成技术深挖报告
    """
    cached = _load_text_checkpoint(checkpoint_dir, "tech_deep_dive")
    if cached is not None:
        if pbar is not None:
            _safe_pbar_update(pbar, pbar.total)
        return cached

    if not innovation_data:
        return "未能提取到核心技术细节。"

    logutil.log(f"\n--- [核心技术细节] 开始深度挖掘 (Model: {model_name}) ---", "INFO", stage="tech_deep_dive")
    sections = []

    summary = _load_text_checkpoint(checkpoint_dir, "tech_deep_dive_00_summary")
    if summary is None:
        summary_prompt = f"请概括本文的**整体技术架构** (Overall Architecture)。\n{prompts.GLOBAL_STYLE_PROMPT}"
        summary = llm_client.call_llm_with_cache(
            context_messages,
            summary_prompt,
            config.API_KEY,
            config.API_URL,
            model_name,
            stage_name="tech_deep_dive.summary"
        )
        summary = utils.correct_image_references(summary, valid_filenames, caption_map)
        _save_text_checkpoint(checkpoint_dir, "tech_deep_dive_00_summary", summary)
    _safe_pbar_update(pbar)
    sections.append(f"### 0. 技术架构概览\n\n{summary}\n")

    indexed_items = list(enumerate(innovation_data, 1))

    def build_detail(indexed_item):
        idx, item = indexed_item
        name = item.get("name", "未知点")
        ctx = item.get("context", "")
        checkpoint_name = f"tech_deep_dive_{idx:02d}_{name}"
        cached_detail = _load_text_checkpoint(checkpoint_dir, checkpoint_name)
        if cached_detail is not None:
            return f"### {idx}. {name}\n\n{cached_detail}\n"

        logutil.log(f"   -> Tech Deep Dive: {name} ...", "INFO", stage="tech_deep_dive")
        prompt = f"""
        任务：深度剖析技术细节 **"{name}"**。
        参考线索：{ctx}
        {prompts.GLOBAL_STYLE_PROMPT}
        要求：
        1. 解释实现原理、算法流程、参数设置。
        2. 说明输入输出关系及在整体中的作用。
        """
        detail = llm_client.call_llm_with_cache(
            context_messages,
            prompt,
            config.API_KEY,
            config.API_URL,
            model_name,
            stage_name=f"tech_deep_dive.{idx}.{name}"
        )
        detail = utils.correct_image_references(detail, valid_filenames, caption_map)
        _save_text_checkpoint(checkpoint_dir, checkpoint_name, detail)
        return f"### {idx}. {name}\n\n{detail}\n"

    sections.extend(item for item in _run_ordered_parallel("tech_deep_dive", indexed_items, build_detail, executor=executor, pbar=pbar) if item)
    result = "\n".join(sections)
    return _save_text_checkpoint(checkpoint_dir, "tech_deep_dive", result)

def analyze_eli5_innovations(context_messages, innovation_data, valid_filenames, model_name, caption_map, checkpoint_dir=None, executor=None, pbar=None):
    """
    [新增] 生成通俗易懂的解释报告
    """
    cached = _load_text_checkpoint(checkpoint_dir, "eli5_notes_body")
    if cached is not None:
        if pbar is not None:
            _safe_pbar_update(pbar, pbar.total)
        return cached

    logutil.log(f"\n--- [通俗解释] 开始生成通俗解释 (Model: {model_name}) ---", "INFO", stage="eli5")
    sections = []

    overall_res = _load_text_checkpoint(checkpoint_dir, "eli5_00_overall")
    if overall_res is None:
        logutil.log("   -> 通俗解释: 整体创新点 ...", "INFO")
        overall_prompt = f"""
        {prompts.ELI5_ROLE_PROMPT}

        任务：请对这篇论文的**核心创新点/整体贡献**进行"直觉性解读"。
        不要陷入细节，而是解释整篇论文主要想解决什么大问题，用了什么巧妙的思路。

        {prompts.GLOBAL_STYLE_PROMPT}
        """
        overall_res = llm_client.call_llm_with_cache(
            context_messages,
            overall_prompt,
            config.API_KEY,
            config.API_URL,
            model_name,
            stage_name="eli5.overall"
        )
        overall_res = utils.correct_image_references(overall_res, valid_filenames, caption_map)
        _save_text_checkpoint(checkpoint_dir, "eli5_00_overall", overall_res)
    _safe_pbar_update(pbar)
    sections.append(f"### 0. 整体创新点通俗解读\n\n{overall_res}\n")

    indexed_items = list(enumerate(innovation_data, 1))

    def build_eli5(indexed_item):
        idx, item = indexed_item
        name = item.get("name", "未知点")
        ctx = item.get("context", "")
        checkpoint_name = f"eli5_{idx:02d}_{name}"
        cached_detail = _load_text_checkpoint(checkpoint_dir, checkpoint_name)
        if cached_detail is not None:
            return f"### {idx}. {name}\n\n{cached_detail}\n"

        logutil.log(f"   -> 通俗解释: {name} ...", "INFO", stage="eli5")
        prompt = f"""
        {prompts.ELI5_ROLE_PROMPT}

        任务：请对技术点 **"{name}"** 进行"直觉性解读"。
        参考线索：{ctx}
        {prompts.GLOBAL_STYLE_PROMPT}
        """
        res = llm_client.call_llm_with_cache(
            context_messages,
            prompt,
            config.API_KEY,
            config.API_URL,
            model_name,
            stage_name=f"eli5.{idx}.{name}"
        )
        res = utils.correct_image_references(res, valid_filenames, caption_map)
        _save_text_checkpoint(checkpoint_dir, checkpoint_name, res)
        return f"### {idx}. {name}\n\n{res}\n"

    sections.extend(item for item in _run_ordered_parallel("eli5_details", indexed_items, build_eli5, executor=executor, pbar=pbar) if item)
    result = "\n".join(sections)
    return _save_text_checkpoint(checkpoint_dir, "eli5_notes_body", result)


def split_markdown_for_translation(full_text, max_chars=8000):
    """按标题切分论文 Markdown 为翻译单元；超长段按段落二次切分，保持 $$...$$ 与图片行完整。"""
    lines = full_text.split("\n")
    blocks, cur = [], []
    for line in lines:
        if re.match(r"^#{1,6}\s", line) and cur:
            blocks.append("\n".join(cur))
            cur = []
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))

    # 超长 block 按段落二次切分
    pieces = []
    for block in blocks:
        block = block.strip("\n")
        if not block:
            continue
        if len(block) <= max_chars:
            pieces.append(block)
            continue
        paras = re.split(r"\n\s*\n", block)
        chunk = ""
        for para in paras:
            if chunk and len(chunk) + len(para) + 2 > max_chars:
                pieces.append(chunk)
                chunk = para
            else:
                chunk = f"{chunk}\n\n{para}" if chunk else para
        if chunk:
            pieces.append(chunk)

    # 过短 piece 向后合并，减少调用数
    segments = []
    for piece in pieces:
        if segments and len(segments[-1]) + len(piece) + 2 <= max_chars:
            segments[-1] = f"{segments[-1]}\n\n{piece}"
        else:
            segments.append(piece)
    return segments


def _split_references_section(full_text):
    """把全文按参考文献章节拆为 (before, refs_section, after)。

    识别优先级：
    1. ``## References`` / ``## Bibliography`` 标题（IGNORECASE）→ 到下一个同级或更高级
       标题、首个独立图片行、或文末。
    2. 无标题时兜底识别行首形如 ``[1] ... [2] ... [3] ...`` 的编号引用条目 run
       （MinerU 输出常无 References 标题，引用条目裸排在文末）。要求连续出现
       1、2、3 才认定，避免与正文枚举列表混淆；多个候选时取最后一个（参考文献必在正文之后）。

    未找到时返回 (full_text, None, "")。
    """
    m = re.search(r"(?m)^(#{1,6})\s+(References|Bibliography)\s*$", full_text, re.IGNORECASE)
    if m:
        heading_level = len(m.group(1))
        start = m.start()
        rest = full_text[m.end():]
        ends = []
        next_heading = re.search(r"(?m)^#{1,%d}\s+\S" % heading_level, rest)
        if next_heading:
            ends.append(next_heading.start())
        next_image = re.search(r"(?m)^!\[", rest)
        if next_image:
            ends.append(next_image.start())
        end = m.end() + min(ends) if ends else len(full_text)
        return full_text[:start], full_text[start:end], full_text[end:]

    # 兜底：无标题，按 [1] [2] [3] ... 编号条目识别
    bracket = re.compile(r"(?m)^\[(\d+)\]\s+\S")
    hits = [(int(g.group(1)), g.start()) for g in bracket.finditer(full_text)]
    # 候选起点：[1] 紧接 [2] 紧接 [3]（序号连续）；取最后一个候选——参考文献必在正文之后
    cand_idx = [i for i in range(len(hits) - 2)
                if hits[i][0] == 1 and hits[i + 1][0] == 2 and hits[i + 2][0] == 3]
    if cand_idx:
        start = hits[cand_idx[-1]][1]
        rest = full_text[start:]
        ends = []
        next_heading = re.search(r"(?m)^#{1,6}\s+\S", rest)
        if next_heading:
            ends.append(next_heading.start())
        next_image = re.search(r"(?m)^!\[", rest)
        if next_image:
            ends.append(next_image.start())
        end = start + min(ends) if ends else len(full_text)
        return full_text[:start], full_text[start:end], full_text[end:]

    return full_text, None, ""


def translate_markdown(full_text, valid_filenames, model_name, checkpoint_dir=None, preserve_references=False, executor=None, pbar=None):
    """将论文原文逐段翻译为简体中文，保留图片/公式/引用/标题结构。走文本供应商。

    preserve_references=True 时，References 章节原文保留、不送 LLM。
    """
    full_ckpt = "translation_full_pr" if preserve_references else "translation_full"
    cached = _load_text_checkpoint(checkpoint_dir, full_ckpt)
    if cached is not None:
        if pbar is not None:
            _safe_pbar_update(pbar, pbar.total)
        return cached

    # 参考文献章节原文保留：把全文拆为 before / refs / after，仅翻译 before+after。
    refs_section = None
    split_idx = None
    seg_prefix = "translation_pr" if preserve_references else "translation"
    if preserve_references:
        before, refs_section, after = _split_references_section(full_text)
        if refs_section is not None:
            before_segs = split_markdown_for_translation(before)
            after_segs = split_markdown_for_translation(after)
            segments = before_segs + after_segs
            split_idx = len(before_segs)
        else:
            segments = split_markdown_for_translation(full_text)
    else:
        segments = split_markdown_for_translation(full_text)

    if refs_section is not None:
        logutil.log(
            f"--- [原文翻译] References 章节原文保留 ({len(refs_section)} 字符)，"
            f"翻译剩余 {len(segments)} 段 (Model: {model_name}) ---", "INFO", stage="translation")
    else:
        logutil.log(f"\n--- [原文翻译] 开始逐段翻译 (Model: {model_name})，共 {len(segments)} 段 ---", "INFO", stage="translation")

    def worker(indexed):
        idx, seg = indexed
        checkpoint_name = f"{seg_prefix}_{idx:02d}"
        cached_seg = _load_text_checkpoint(checkpoint_dir, checkpoint_name)
        if cached_seg is not None:
            return cached_seg
        logutil.log(f"   -> 翻译第 {idx}/{len(segments)} 段 ...", "INFO", stage="translation")
        messages = [
            {"role": "system", "content": config.UNIFIED_SYSTEM_PROMPT},
            {"role": "user", "content": utils.format_document_content(seg)},
        ]
        res = llm_client.call_llm_with_cache(
            messages,
            prompts.TRANSLATION_PROMPT,
            config.API_KEY,
            config.API_URL,
            model_name,
            stage_name=f"translation.{idx}",
            strip_headings=False,
        )
        res = utils.correct_image_references(res, valid_filenames, None)
        return _save_text_checkpoint(checkpoint_dir, checkpoint_name, res)

    indexed_items = list(enumerate(segments, 1))
    translated = _run_ordered_parallel("translation", indexed_items, worker, executor=executor, pbar=pbar)
    if refs_section is not None:
        before_result = "\n\n".join(t for t in translated[:split_idx] if t)
        after_result = "\n\n".join(t for t in translated[split_idx:] if t)
        parts = []
        if before_result:
            parts.append(before_result)
        parts.append(refs_section)
        if after_result:
            parts.append(after_result)
        result = "\n\n".join(parts)
    else:
        result = "\n\n".join(t for t in translated if t)
    return _save_text_checkpoint(checkpoint_dir, full_ckpt, result)


def generate_info_json_data(context_messages, model_name, additional_context=None, checkpoint_dir=None):
    """
    生成 info.json 所需的元数据和描述信息
    """
    cached = _load_json_checkpoint(checkpoint_dir, "info_data")
    if cached is not None:
        return cached

    logutil.log(f"\n--- [信息提取] 正在生成元数据和描述 (Model: {model_name}) ---", "INFO")

    enhanced_messages = copy.deepcopy(context_messages)
    if additional_context:
        enhanced_messages.append({"role": "user", "content": additional_context})

    json_str = llm_client.call_llm_with_cache(
        enhanced_messages,
        prompts.INFO_JSON_PROMPT,
        config.API_KEY,
        config.API_URL,
        model_name,
        json_mode=True,
        stage_name="info_json"
    )

    if not json_str:
        return None

    try:
        clean = json_str.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean)
        return _save_json_checkpoint(checkpoint_dir, "info_data", data)
    except json.JSONDecodeError as e:
        logutil.log(f"[解析错误] JSON 解析失败: {e}\n原始内容: {json_str}", "ERROR")
        return None
    except Exception as e:
        logutil.log(f"[未知错误] {e}", "ERROR")
        return None

def analyze_section(title, task_prompt, context_messages, valid_filenames, model_name, caption_map, checkpoint_dir=None, checkpoint_name=None):
    """
    分析论文的特定章节
    """
    checkpoint_name = checkpoint_name or f"section_{title}"
    cached = _load_text_checkpoint(checkpoint_dir, checkpoint_name)
    if cached is not None:
        return cached

    logutil.log(f"--- 正在分析: {title} ---", "INFO", stage=checkpoint_name or f"section_{title}")
    full_prompt = f"{task_prompt}\n{prompts.GLOBAL_STYLE_PROMPT}"
    res = llm_client.call_llm_with_cache(
        context_messages,
        full_prompt,
        config.API_KEY,
        config.API_URL,
        model_name,
        stage_name=f"section.{title}"
    )
    res = utils.correct_image_references(res, valid_filenames, caption_map)
    return _save_text_checkpoint(checkpoint_dir, checkpoint_name, res)

def analyze_single_figure_isolated(image_path, full_text, valid_filenames, model_name, caption=None, checkpoint_dir=None):
    """
    单独分析单个图片
    """
    filename = os.path.basename(image_path)
    checkpoint_name = f"figure_{filename}"
    cached = _load_text_checkpoint(checkpoint_dir, checkpoint_name)
    if cached is not None:
        return cached

    logutil.log(f"   -> 单图分析: {filename} ...", "INFO", stage="figures")

    prompt = f"""
    任务：详细分析图片 {filename}。
    图片说明：{caption or ''}
    {prompts.FIGURE_STYLE_PROMPT}
    """

    msgs = [{"role": "system", "content": config.UNIFIED_SYSTEM_PROMPT}]

    content = [
        {
            "type": "text",
            "text": utils.format_document_content(full_text),
            "cache_control": {"type": "ephemeral"}
        },
        {"type": "text", "text": f"Target Image: {filename}"},
        {
            "type": "image_url",
            "image_url": {"url": utils.encode_image_to_base64(image_path), "detail": "high"}
        },
        {"type": "text", "text": prompt}
    ]
    msgs.append({"role": "user", "content": content})

    try:
        raw = llm_client.call_llm_with_cache(
            msgs,
            [],
            config.IMAGE_API_KEY,
            config.IMAGE_API_URL,
            model_name,
            stage_name=f"figure.{filename}",
            wire_api=config.IMAGE_WIRE_API,
        )
        result = utils.correct_image_references(raw, valid_filenames, None)
        return _save_text_checkpoint(checkpoint_dir, checkpoint_name, result)
    except Exception as e:
        logutil.log(f"图表分析失败: {e}", "ERROR", stage="figures")
        return None
