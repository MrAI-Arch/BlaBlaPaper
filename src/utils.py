"""
工具函数模块 - 提供纯函数工具，不依赖业务逻辑
"""
import re
import base64
import mimetypes
import os
import zipfile

from . import logutil


def generate_slug_from_title(title):
    """
    从论文标题生成 URL 友好的 slug

    Args:
        title: 论文标题字符串

    Returns:
        小写、连字符分隔的 slug 字符串
    """
    if not title:
        return "processed_report"

    slug = title.lower()
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'[^a-z0-9\-]', '', slug)
    return slug.strip('-')


def encode_image_to_base64(image_path):
    """
    将图片文件编码为 base64 Data URI

    Args:
        image_path: 图片文件的绝对路径

    Returns:
        base64 编码的 Data URI 字符串 (格式: data:image/png;base64,...)
    """
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type is None:
        mime_type = "application/octet-stream"

    with open(image_path, "rb") as f:
        return f"data:{mime_type};base64,{base64.b64encode(f.read()).decode('utf-8')}"


def clean_llm_output(text, strip_headings=True):
    """
    清理 LLM 输出中的 markdown 代码块标记和标题语法

    Args:
        text: LLM 原始输出文本

    Returns:
        清理后的纯文本，移除 ``` 标记和所有 # 标题
    """
    if not text:
        return text

    # 移除 markdown 代码块标记
    text = re.sub(r'^```(markdown)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text)

    # 仅在分析报告等"禁止标题"场景下剥离标题；翻译等需保留标题时跳过
    if not strip_headings:
        return text.strip()

    # 移除所有位置的 Markdown 标题（包括列表项中的标题，如 "- ## xxxx"）
    lines = text.split('\n')
    cleaned_lines = []

    for line in lines:
        # 检查是否包含标题标记（# 后面跟空格）
        heading_match = re.search(r'#+\s+', line)
        if heading_match:
            # 检查是否是列表项中的标题（如 "- ## 标题"）
            list_match = re.match(r'^(\s*[-*+]\s+)', line)
            if list_match:
                # 保留列表标记，去除标题标记（#），保留标题文本
                list_prefix = list_match.group(1)
                after_list = line[len(list_prefix):]
                # 去除 # 标记，保留后面的文本
                after_heading = re.sub(r'#+\s+', '', after_list, count=1)
                cleaned_lines.append(list_prefix + after_heading)
            else:
                # 完全去除标题行（不保留）
                continue
        else:
            # 不包含标题标记，保留原样
            cleaned_lines.append(line)

    text = '\n'.join(cleaned_lines)
    return text.strip()


def correct_image_references(text, valid_filenames, caption_map=None):
    """
    修正文本中的图片引用路径，并可选添加图片说明

    Args:
        text: 包含图片引用的文本
        valid_filenames: 有效的图片文件名集合 (set)
        caption_map: 可选，图片文件名到说明的映射字典

    Returns:
        修正后的文本，图片路径统一为 images/文件名 格式
    """
    if not text:
        return text

    # 移除图片引用周围的反引号
    text = re.sub(r'`\s*(!\[.*?\]\(.*?\))\s*`', r'\1', text)

    def replace_link(match):
        full_match = match.group(0)
        alt = match.group(1)
        path = match.group(2)
        fname = os.path.basename(path)

        if fname in valid_filenames:
            new_md = f"![{alt}](images/{fname})"
            if caption_map and fname in caption_map:
                new_md += f" *{caption_map[fname]}*"
            return new_md
        return full_match

    return re.sub(r'!\[(.*?)\]\((.*?)\)', replace_link, text)


def format_document_content(text_content):
    """
    格式化文档内容为带标签的结构

    Args:
        text_content: 原始文档文本内容

    Returns:
        带有 <Document_Content> 标签的格式化文本
    """
    return f"<Document_Content>\n{text_content}\n</Document_Content>\n\n"


def extract_zip(zip_path, extract_to_dir):
    """
    解压 ZIP 文件到指定目录

    Args:
        zip_path: ZIP 文件路径
        extract_to_dir: 解压目标目录

    Returns:
        str: 解压后的目录路径

    Raises:
        FileNotFoundError: ZIP 文件不存在时抛出异常
        Exception: 解压失败时抛出异常
    """
    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"ZIP 文件不存在: {zip_path}")

    # 创建目标目录（如果不存在）
    os.makedirs(extract_to_dir, exist_ok=True)

    # 解压文件
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_to_dir)

    logutil.log(f"✅ 解压完成: {extract_to_dir}", "INFO")
    return extract_to_dir
