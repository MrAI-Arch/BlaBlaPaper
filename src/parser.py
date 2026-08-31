"""
文件解析模块 - 处理本地文件读取、PyMuPDF 操作和图片处理
"""
import os
import glob
import json
import re
import shutil
import sys

from . import logutil

# 可选依赖：PyMuPDF
try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False
    logutil.log("[警告] PyMuPDF 未安装，将无法提取 PDF 元数据。请运行: pip install PyMuPDF", "WARN")


def extract_images_from_markdown(md_content, base_dir):
    """
    从 Markdown 内容中提取图片引用路径

    Args:
        md_content: Markdown 文本内容
        base_dir: 图片文件的基础目录路径

    Returns:
        按顺序排列的图片文件绝对路径列表
    """
    logutil.log("--- 正在扫描 Markdown 中的图片引用 ---", "INFO")
    img_refs = re.findall(r'!\[.*?\]\((.*?)\)', md_content)
    ordered_source_paths = []

    for ref in img_refs:
        ref = ref.strip()
        full_path = os.path.join(base_dir, ref)
        if os.path.exists(full_path):
            ordered_source_paths.append(full_path)

    return ordered_source_paths


def setup_environment(output_dir, ordered_source_paths):
    """
    创建输出目录并复制图片文件

    Args:
        output_dir: 输出目录路径
        ordered_source_paths: 按顺序排列的图片源文件路径列表

    Returns:
        tuple: (新图片路径列表, 有效文件名集合)
    """
    images_subdir = os.path.join(output_dir, "images")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    if not os.path.exists(images_subdir):
        os.makedirs(images_subdir)

    new_image_paths = []
    valid_filenames = set()
    processed_filenames = set()

    for src_path in ordered_source_paths:
        filename = os.path.basename(src_path)
        dst_path = os.path.join(images_subdir, filename)
        valid_filenames.add(filename)

        if filename not in processed_filenames:
            shutil.copy2(src_path, dst_path)
            processed_filenames.add(filename)

        new_image_paths.append(dst_path)

    return new_image_paths, valid_filenames


def get_paper_info(input_dir):
    """
    从输入目录的 *_content_list.json 文件中提取论文标题和图片说明

    Args:
        input_dir: 输入目录路径

    Returns:
        tuple: (论文标题, 图片说明映射字典)
    """
    json_files = glob.glob(os.path.join(input_dir, "*_content_list.json"))
    if not json_files:
        return None, {}

    paper_title = None
    caption_map = {}

    try:
        with open(json_files[0], 'r', encoding='utf-8') as f:
            data = json.load(f)

        # 提取论文标题（第一个 type="text" 的文本）
        for item in data:
            if item.get("type") == "text":
                raw = item.get("text", "").strip()
                if raw:
                    paper_title = raw
                    break

        # 提取图片说明
        for item in data:
            if 'img_path' in item:
                fname = os.path.basename(item['img_path'])
                caption = " ".join(
                    item.get('image_caption', []) or item.get('table_caption', [])
                )
                if caption:
                    caption_map[fname] = re.sub(r'\s+', ' ', caption).strip()

    except Exception as e:
        logutil.log(f"[警告] 元数据解析失败: {e}", "WARN")

    return paper_title, caption_map


def extract_discarded_text(input_dir):
    """
    从 *_content_list.json 文件中提取类型为 discarded 的文本块

    Args:
        input_dir: 输入目录路径

    Returns:
        拼接后的 discarded 文本字符串，未找到时返回 None
    """
    json_files = glob.glob(os.path.join(input_dir, "*_content_list.json"))
    if not json_files:
        return None

    try:
        with open(json_files[0], 'r', encoding='utf-8') as f:
            data = json.load(f)

        discarded_texts = []
        for item in data:
            if item.get("type") == "discarded":
                text = item.get("text", "").strip()
                if text:
                    discarded_texts.append(text)

        if discarded_texts:
            return "\n".join(discarded_texts)
        return None

    except Exception as e:
        logutil.log(f"[警告] 提取 discarded 文本失败: {e}", "WARN")
        return None


def get_pdf_metadata_context(input_dir):
    """
    从输入目录中查找 *_origin.pdf 文件并提取元数据，
    同时提取 discarded 文本，格式化为追加上下文供 LLM 使用

    Args:
        input_dir: 输入目录路径

    Returns:
        格式化的补充信息字符串（包含 PDF 元数据和 discarded 文本），
        未找到时返回 None
    """
    context_parts = []

    # 1. 提取 PDF 元数据
    if HAS_PYMUPDF:
        pdf_files = glob.glob(os.path.join(input_dir, "*_origin.pdf"))
        if pdf_files:
            pdf_path = pdf_files[0]  # 使用第一个找到的文件
            try:
                doc = fitz.open(pdf_path)

                metadata_text = []
                metadata_text.append("=== PDF 文档元数据 (Document Information Dictionary) ===\n")

                # Document Information Dictionary
                metadata = doc.metadata
                for key, value in metadata.items():
                    if value and value.strip():  # 只添加非空值
                        metadata_text.append(f"{key}: {value}")

                metadata_text.append("\n")

                # XMP Metadata
                xmp_content = doc.get_xml_metadata()
                if xmp_content:
                    metadata_text.append("=== PDF XMP 元数据 (XML Metadata) ===\n")
                    metadata_text.append(f"XMP 数据长度: {len(xmp_content)} 字符\n")
                    # 添加完整的 XMP 内容，让 LLM 可以解析
                    metadata_text.append(xmp_content)

                doc.close()

                pdf_metadata = "\n".join(metadata_text) if metadata_text else None
                if pdf_metadata:
                    context_parts.append(pdf_metadata)
                    logutil.log("   -> 已提取 PDF 元数据", "INFO")

            except Exception as e:
                logutil.log(f"[警告] PDF 元数据提取失败: {e}", "WARN")

    # 2. 提取 discarded 文本
    discarded_text = extract_discarded_text(input_dir)
    if discarded_text:
        context_parts.append(
            f"=== 文档中被丢弃的文本块 (Discarded Text Blocks) ===\n\n{discarded_text}"
        )
        logutil.log("   -> 已提取 discarded 文本块", "INFO")

    # 3. 合并所有补充信息
    if context_parts:
        combined_context = "\n\n".join(context_parts)
        logutil.log("   -> 将补充信息添加到上下文", "INFO")
        return (
            f"=== 补充信息 ===\n\n{combined_context}\n\n"
            "请参考上述补充信息（PDF 元数据和文档中被丢弃的文本块）"
            "来提取论文的基本信息（作者、发表期刊/会议、年份等）。"
        )

    return None
