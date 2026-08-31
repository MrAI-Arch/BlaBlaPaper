#!/usr/bin/env python3
"""
Paper Analyzer - 论文分析工具主入口
模块化重构版本
"""
import argparse
import threading
import os
import sys
import shutil
import glob
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from src import logutil

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


REPORT_MARKDOWN_FILES = ("paper_notes.md", "ELI5_notes.md", "figs_notes.md", "translation_notes.md")


REPORT_SECTION_ORDER = (
    "0. 论文基本信息",
    "1. 摘要",
    "2. 背景知识与核心贡献",
    "3. 核心技术和实现细节",
    "4. 实验方法与实验结果",
)


def log(message, level="INFO", stage=None):
    logutil.log(message, level, stage=stage)


def write_progress(output_dir, stage, status="running", error=None):
    if not output_dir:
        return
    try:
        os.makedirs(output_dir, exist_ok=True)
        payload = {
            "stage": stage,
            "status": status,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if error:
            payload["error"] = str(error)
        with open(os.path.join(output_dir, "progress.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def write_paper_report(output_dir, main_title, results, filename="paper_notes.partial.md"):
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {main_title} 论文解析\n\n")
        for key in REPORT_SECTION_ORDER:
            if key in results:
                f.write(f"## {key}\n\n{results[key]}\n\n---\n\n")


def write_eli5_report(output_dir, title, content, filename="ELI5_notes.partial.md"):
    with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write(content or "")


def write_fig_report(output_dir, title, figure_sections, filename="figs_notes.partial.md"):
    with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
        f.write(f"# {title} 图表详解\n\n")
        for section in figure_sections:
            if section:
                f.write(section)


def write_translation_report(output_dir, title, content, filename="translation_notes.partial.md"):
    with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write(content or "")


def remove_partial_report(output_dir, basename):
    """最终稿写完后，删除对应的 *.partial.md 中间文件（崩溃兜底用，定稿后冗余）。"""
    partial = os.path.join(output_dir, f"{basename}.partial.md")
    if os.path.exists(partial):
        os.remove(partial)


def is_existing_report_dir(path):
    """判断目录是否是已经生成过的 BlaBlaCutter 输出目录。"""
    return os.path.isdir(path) and any(
        os.path.exists(os.path.join(path, name))
        for name in REPORT_MARKDOWN_FILES
    )


def main():
    """主函数：执行完整的论文分析流程"""
    # 解析命令行参数
    parser_cli = argparse.ArgumentParser(description="Paper Analyzer - 论文分析工具")
    parser_cli.add_argument("input_path", help="输入路径：PDF 文件或包含 .md 文件的目录")
    parser_cli.add_argument(
        "-html",
        "--html",
        action="store_true",
        help="额外导出 HTML 版本报告",
    )
    parser_cli.add_argument(
        "--html-only",
        action="store_true",
        help="只把已有 Markdown 报告导出为 HTML，不重新分析论文",
    )
    parser_cli.add_argument(
        "--pages-dir",
        help="额外生成 GitHub Pages 可直接发布的静态目录，例如 docs",
    )
    parser_cli.add_argument(
        "--text-model",
        dest="text_model",
        help="覆盖文本分析所用模型（默认读 .env 的 model）",
    )
    parser_cli.add_argument(
        "--image-model",
        dest="image_model",
        help="覆盖逐图分析所用模型（默认读 .env 的 model_image，未设则同 text 模型）",
    )
    parser_cli.add_argument(
        "--yes",
        action="store_true",
        help="跳过 TeX 模式的交互确认（如检测到 TikZ 图时自动继续）",
    )
    parser_cli.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="打印调试级日志（每次 LLM 调用的完整信息、checkpoint/parallel 等）",
    )
    args = parser_cli.parse_args()

    if args.verbose or os.getenv("BLABLA_VERBOSE", "").strip().lower() in ("1", "true", "yes"):
        logutil.set_verbosity("DEBUG")
    run_started = time.monotonic()

    input_path = os.path.expanduser(args.input_path)
    pages_dir = os.path.expanduser(args.pages_dir) if args.pages_dir else None
    should_export_html = args.html or args.html_only or bool(pages_dir)
    if not os.path.exists(input_path):
        log(f"[错误] 输入路径不存在: {input_path}", "ERROR")
        sys.exit(1)

    # 允许通过命令行覆盖 .env 中的模型选择（CLI 优先于配置）
    if args.text_model or args.image_model:
        from src import config
        if args.text_model:
            config.MODEL_NAME_TEXT = args.text_model
        if args.image_model:
            config.MODEL_NAME_IMAGE = args.image_model

    if should_export_html and is_existing_report_dir(input_path):
        from src import html_exporter

        html_index = html_exporter.export_html_reports(
            input_path,
            site_title="BlaBlaCutter",
            output_root=pages_dir,
        )
        log(f"✅ HTML 报告已保存: {html_index}")
        if pages_dir:
            log(f"✅ GitHub Pages 静态目录已保存: {os.path.abspath(pages_dir)}")
        return

    if args.html_only:
        log(f"[错误] --html-only 需要传入已生成的报告目录: {input_path}", "ERROR")
        log("提示：目录中应包含 paper_notes.md、ELI5_notes.md 或 figs_notes.md", "ERROR")
        sys.exit(1)

    from src import config
    from src import parser
    from src import core
    from src import utils
    from src import mineru_client
    from src import html_exporter
    from src import tex_loader
    from src import llm_client

    # 判断输入类型并处理
    zip_path = None  # 跟踪临时 ZIP 文件（用于清理）
    is_pdf_input = False  # 仅 PDF 模式才重命名输入目录
    is_tex_input = False  # TeX 模式产生临时工作目录，需在 finally 清理
    tex_work_dir = None  # TeX 转换出的临时工作目录路径

    text_executor = None    # 文本模型线程池
    image_executor = None   # 图片模型线程池

    try:
        if os.path.isfile(input_path) and input_path.lower().endswith('.pdf'):
            # === 模式 B: PDF 输入 ===
            log(f"📄 检测到 PDF 文件输入: {input_path}")

            # 初始化 Mineru 客户端
            client = mineru_client.MineruClient()

            # 1. 上传 PDF
            batch_id = client.upload_file(input_path)
            if not batch_id:
                log("❌ PDF 上传失败", "ERROR")
                sys.exit(1)

            # 2. 轮询解析状态
            download_url = client.poll_status(batch_id)
            if not download_url:
                log("❌ PDF 解析失败", "ERROR")
                sys.exit(1)

            # 3. 下载结果 ZIP
            pdf_dir = os.path.dirname(input_path)
            pdf_name = os.path.splitext(os.path.basename(input_path))[0]
            zip_path = os.path.join(pdf_dir, f"{pdf_name}_result.zip")

            if not client.download_file(download_url, zip_path):
                log("❌ 结果下载失败", "ERROR")
                sys.exit(1)

            # 4. 解压 ZIP 文件
            extract_dir = os.path.join(pdf_dir, f"{pdf_name}_extracted")
            utils.extract_zip(zip_path, extract_dir)

            # 设置工作目录为解压后的目录
            work_dir = extract_dir
            is_pdf_input = True
            log(f"✅ PDF 解析完成，工作目录: {work_dir}\n")

        elif os.path.isdir(input_path):
            # === 目录输入：TeX 源码优先转换，否则按 MinerU 产物处理 ===
            if tex_loader.is_tex_source(input_path):
                log(f"📄 检测到 TeX 源码输入: {input_path}")
                work_dir = tex_loader.convert(input_path, assume_yes=args.yes)
                is_tex_input = True
                tex_work_dir = work_dir
                log(f"✅ TeX 已转换为 Markdown，工作目录: {work_dir}\n")
            else:
                log(f"📁 检测到目录输入: {input_path}")
                work_dir = input_path

        else:
            log(f"[错误] 不支持的输入类型: {input_path}", "ERROR")
            log("提示：请提供 PDF 文件或包含 .md 文件的目录", "ERROR")
            sys.exit(1)

        # === 后续流程统一使用 work_dir ===
        INPUT_DIR = work_dir

        # 查找 Markdown 文件
        md_files = glob.glob(os.path.join(INPUT_DIR, "*.md"))
        if not md_files:
            log(f"[错误] 未在目录中找到 .md 文件: {INPUT_DIR}", "ERROR")
            sys.exit(1)

        # 优先使用 full.md 或 md.md，否则使用第一个 .md 文件
        md_source = next(
            (p for p in [os.path.join(INPUT_DIR, "full.md"), os.path.join(INPUT_DIR, "md.md")]
             if os.path.exists(p)),
            md_files[0]
        )

        # 读取论文文本内容
        with open(md_source, 'r', encoding='utf-8') as f:
            full_text = f.read()

        # 提取图片引用和论文元信息
        ordered_imgs = parser.extract_images_from_markdown(full_text, INPUT_DIR)
        paper_title, caption_map = parser.get_paper_info(INPUT_DIR)

        # 用 full.md 首个 # 标题校正：content_list 首个 text 可能是版权声明等噪声
        for _line in full_text.splitlines():
            if _line.startswith('# '):
                paper_title = _line[2:].strip()
                break

        # 生成输出目录名称
        output_subdir = utils.generate_slug_from_title(paper_title) if paper_title else f"Report_{os.path.basename(INPUT_DIR.strip('/'))}"
        OUTPUT_DIR = os.path.join("outputs", output_subdir)

        log(f"--- Output: {OUTPUT_DIR} ---")

        # 创建输出环境并复制图片
        new_img_paths, valid_filenames = parser.setup_environment(OUTPUT_DIR, ordered_imgs)

        # 输出目录就绪后，把日志写入 run-<时间戳>.log（保留历史）。
        # 此前的早期日志已缓冲，set_log_file 时一并刷盘。
        logutil.set_log_file(os.path.join(OUTPUT_DIR, f"run-{time.strftime('%Y%m%d-%H%M%S')}.log"))

        # 构建完整的 LLM 上下文
        # 避免每个章节请求都重复发送全部图片；逐图分析仍会单独使用图片。
        include_full_context_images = os.getenv(
            "INCLUDE_FULL_CONTEXT_IMAGES", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        full_context_image_paths = new_img_paths if include_full_context_images else []
        full_context = core.build_full_context(full_text, full_context_image_paths)

        # 执行分析流程
        results = {}

        # 提前定义 main_title，供后续使用
        main_title = paper_title.replace('\n', ' ') if paper_title else "论文分析报告"
        checkpoint_dir = os.path.join(OUTPUT_DIR, ".checkpoints")
        os.makedirs(checkpoint_dir, exist_ok=True)
        write_progress(OUTPUT_DIR, "start")

        # 生成并保存 info.json（提前生成，供基本信息提取使用）
        if is_tex_input:
            pdf_metadata_context = tex_loader.build_meta_context(INPUT_DIR)
        else:
            pdf_metadata_context = parser.get_pdf_metadata_context(INPUT_DIR)
        write_progress(OUTPUT_DIR, "info_json")
        info_data = core.generate_info_json_data(
            full_context,
            config.MODEL_NAME_TEXT,
            pdf_metadata_context,
            checkpoint_dir=checkpoint_dir,
        )

        # 立即保存 info.json
        if info_data:
            final_info = {
                "index": output_subdir,
                "paper_title": main_title,
                "metadata": info_data.get("metadata", {}),
                "description": info_data.get("description", "")
            }
            with open(os.path.join(OUTPUT_DIR, "info.json"), "w", encoding="utf-8") as f:
                json.dump(final_info, f, ensure_ascii=False, indent=2)
            log("✅ 信息文件已保存: info.json")
        else:
            log("⚠️  信息提取失败，跳过 info.json 生成", "WARN")

        results["0. 论文基本信息"] = core.analyze_bibliographic_info(info_data)
        write_paper_report(OUTPUT_DIR, main_title, results)

        # 独立的章节分析和技术点提取可以并发执行。
        analysis_tasks = [
            (
                "section",
                "1. 摘要",
                lambda: core.analyze_section(
                    "摘要",
                    "撰写结构化摘要：目的、方法、结果、结论。",
                    full_context,
                    valid_filenames,
                    config.MODEL_NAME_TEXT,
                    caption_map,
                    checkpoint_dir=checkpoint_dir,
                    checkpoint_name="section_summary",
                ),
            ),
            (
                "section",
                "2. 背景知识与核心贡献",
                lambda: core.analyze_section(
                    "背景与贡献",
                    "概括研究背景、动机及核心贡献。",
                    full_context,
                    valid_filenames,
                    config.MODEL_NAME_TEXT,
                    caption_map,
                    checkpoint_dir=checkpoint_dir,
                    checkpoint_name="section_background_contribution",
                ),
            ),
            (
                "section",
                "4. 实验方法与实验结果",
                lambda: core.analyze_section(
                    "实验",
                    "分析实验设置、结果数据、消融实验。",
                    full_context,
                    valid_filenames,
                    config.MODEL_NAME_TEXT,
                    caption_map,
                    checkpoint_dir=checkpoint_dir,
                    checkpoint_name="section_experiments",
                ),
            ),
            (
                "tech_points",
                "tech_points",
                lambda: core.extract_tech_points(
                    full_context,
                    config.MODEL_NAME_TEXT,
                    checkpoint_dir=checkpoint_dir,
                ),
            ),
       ]

        tech_points = []
        text_executor = ThreadPoolExecutor(max_workers=config.TEXT_MAX_WORKERS)
        image_executor = ThreadPoolExecutor(max_workers=config.IMAGE_MAX_WORKERS)
        logutil.set_bars_active(True)
        _bar_fmt = "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]"
        pbar_p1 = tqdm(total=len(analysis_tasks), desc="通读分析", position=0,
                       dynamic_ncols=True, leave=False, bar_format=_bar_fmt) if tqdm else None
        write_progress(OUTPUT_DIR, "main_analysis")
        try:
            future_to_task = {
                text_executor.submit(task_fn): (task_type, key)
                for task_type, key, task_fn in analysis_tasks
            }
            for future in as_completed(future_to_task):
                task_type, key = future_to_task[future]
                try:
                    value = future.result()
                except Exception as e:
                    log(f"[parallel] main_analysis: {key} failed: {e}", "ERROR", stage="main_analysis")
                    value = [] if task_type == "tech_points" else None

                if task_type == "tech_points":
                    tech_points = value or []
                    logutil.write(f"[parallel] main_analysis: tech_points done count={len(tech_points)}")
                else:
                    results[key] = value
                    write_paper_report(OUTPUT_DIR, main_title, results)
                    log(f"[parallel] main_analysis: {key} done", "DEBUG", stage="main_analysis")
                if pbar_p1 is not None:
                    try:
                        pbar_p1.update(1)
                    except (BrokenPipeError, OSError, IOError):
                        pass
        finally:
            if pbar_p1 is not None:
                try:
                    pbar_p1.close()
                except (BrokenPipeError, OSError, IOError):
                    pass
        logutil.set_bars_active(False)

        # === Phase 2: 深挖 / ELI5 / 翻译 / 图表 四阶段并发 ===
        # 都只依赖 Phase 1 的 tech_points / full_text / images，互不依赖。
        write_progress(OUTPUT_DIR, "phase2")
        unique_imgs = sorted(list(set(new_img_paths)), key=new_img_paths.index)
        n_deep = len(tech_points) + 1
        n_eli5 = len(tech_points) + 1

        before, refs_section, after = core._split_references_section(full_text)
        if refs_section is not None:
            n_trans = len(core.split_markdown_for_translation(before)) + \
                      len(core.split_markdown_for_translation(after))
        else:
            n_trans = len(core.split_markdown_for_translation(full_text))
        n_fig = len(unique_imgs)

        logutil.set_bars_active(True)
        pbar_deep = tqdm(total=n_deep, desc="深挖   ", position=0,
                         dynamic_ncols=True, leave=True, bar_format=_bar_fmt) if tqdm and n_deep else None
        pbar_eli5 = tqdm(total=n_eli5, desc="ELI5  ", position=1,
                         dynamic_ncols=True, leave=True, bar_format=_bar_fmt) if tqdm and n_eli5 else None
        pbar_trans = tqdm(total=n_trans, desc="翻译  ", position=2,
                          dynamic_ncols=True, leave=True, bar_format=_bar_fmt) if tqdm and n_trans else None
        pbar_fig = tqdm(total=n_fig, desc="图表  ", position=3,
                        dynamic_ncols=True, leave=True, bar_format=_bar_fmt) if tqdm and n_fig else None

        phase2_errors = []
        eli5_content = [None]
        translation_content = [None]
        figure_sections = [None] * len(unique_imgs)

        def run_deep_dive():
            try:
                results["3. 核心技术和实现细节"] = core.generate_tech_deep_dive(
                    full_context, tech_points, valid_filenames,
                    config.MODEL_NAME_TEXT, caption_map,
                    checkpoint_dir=checkpoint_dir,
                    executor=text_executor, pbar=pbar_deep,
                )
                write_paper_report(OUTPUT_DIR, main_title, results)
                write_paper_report(OUTPUT_DIR, main_title, results, filename="paper_notes.md")
                remove_partial_report(OUTPUT_DIR, "paper_notes")
                logutil.write("✅ 主报告已保存")
            except Exception as e:
                phase2_errors.append(("deep_dive", e))
                log(f"❌ 深挖阶段失败: {e}", "ERROR", stage="tech_deep_dive")

        def run_eli5():
            try:
                eli5_content[0] = core.analyze_eli5_innovations(
                    full_context, tech_points, valid_filenames,
                    config.MODEL_NAME_TEXT, caption_map,
                    checkpoint_dir=checkpoint_dir,
                    executor=text_executor, pbar=pbar_eli5,
                )
                eli5_title = f"{main_title} 通俗讲解"
                write_eli5_report(OUTPUT_DIR, eli5_title, eli5_content[0])
                write_eli5_report(OUTPUT_DIR, eli5_title, eli5_content[0], filename="ELI5_notes.md")
                remove_partial_report(OUTPUT_DIR, "ELI5_notes")
                logutil.write("✅ 通俗解释报告已保存: ELI5_notes.md")
            except Exception as e:
                phase2_errors.append(("eli5", e))
                log(f"❌ ELI5 阶段失败: {e}", "ERROR", stage="eli5")

        def run_translation():
            try:
                translation_content[0] = core.translate_markdown(
                    full_text, valid_filenames, config.MODEL_NAME_TEXT,
                    checkpoint_dir=checkpoint_dir,
                    preserve_references=True,
                    executor=text_executor, pbar=pbar_trans,
                )
                translation_title = f"{main_title} 原文翻译"
                write_translation_report(OUTPUT_DIR, translation_title, translation_content[0])
                write_translation_report(OUTPUT_DIR, translation_title, translation_content[0], filename="translation_notes.md")
                remove_partial_report(OUTPUT_DIR, "translation_notes")
                logutil.write("✅ 原文翻译报告已保存: translation_notes.md")
            except Exception as e:
                phase2_errors.append(("translation", e))
                log(f"❌ 翻译阶段失败: {e}", "ERROR", stage="translation")

        def run_figures():
            try:
                def analyze_figure(indexed):
                    idx, img_path = indexed
                    fname = os.path.basename(img_path)
                    caption = caption_map.get(fname, "")
                    res = core.analyze_single_figure_isolated(
                        img_path, full_text, valid_filenames,
                        config.MODEL_NAME_IMAGE, caption,
                        checkpoint_dir=checkpoint_dir,
                    )
                    if not res:
                        return idx, None
                    section = f"### {caption or fname}\n\n![{fname}](images/{fname})\n\n{res}\n\n"
                    return idx, section

                indexed_items = list(enumerate(unique_imgs))
                results = core._run_ordered_parallel(
                    "figures", indexed_items, analyze_figure,
                    executor=image_executor, pbar=pbar_fig,
                )
                for idx, section in results:
                    if section:
                        figure_sections[idx] = section
                write_fig_report(OUTPUT_DIR, main_title, figure_sections, filename="figs_notes.md")
                remove_partial_report(OUTPUT_DIR, "figs_notes")
                logutil.write("✅ 图表报告已保存")
            except Exception as e:
                phase2_errors.append(("figures", e))
                log(f"❌ 图表阶段失败: {e}", "ERROR", stage="figures")

        t_deep = threading.Thread(target=run_deep_dive, name="phase2-deep_dive")
        t_eli5 = threading.Thread(target=run_eli5, name="phase2-eli5")
        t_trans = threading.Thread(target=run_translation, name="phase2-translation")
        t_fig = threading.Thread(target=run_figures, name="phase2-figures")
        t_deep.start()
        t_eli5.start()
        t_trans.start()
        t_fig.start()
        t_deep.join()
        t_eli5.join()
        t_trans.join()
        t_fig.join()

        for pbar in (pbar_deep, pbar_eli5, pbar_trans, pbar_fig):
            if pbar is not None:
                try:
                    pbar.close()
                except (BrokenPipeError, OSError, IOError):
                    pass

        logutil.set_bars_active(False)
        text_executor.shutdown(wait=True)
        image_executor.shutdown(wait=True)
        write_progress(OUTPUT_DIR, "reports", status="done")

        # 运行摘要
        total_elapsed = time.monotonic() - run_started
        total_calls, failed_calls = llm_client.stats()
        log(f"✅ 完成: {main_title} | 耗时 {total_elapsed:.1f}s | LLM 调用 {total_calls} 次（失败 {failed_calls}）")

        # 可选：导出 HTML 版本
        if args.html or pages_dir:
            html_index = html_exporter.export_html_reports(
                OUTPUT_DIR,
                site_title="BlaBlaCutter",
                output_root=pages_dir,
            )
            log(f"✅ HTML 报告已保存: {html_index}")
            if pages_dir:
                log(f"✅ GitHub Pages 静态目录已保存: {os.path.abspath(pages_dir)}")

        # 重命名输入文件夹（仅 PDF 模式：解压目录一次性；目录/.md 输入是用户指定路径，不动）
        if is_pdf_input and output_subdir and paper_title:
            try:
                input_dir_abs = os.path.abspath(INPUT_DIR)
                input_parent = os.path.dirname(input_dir_abs)
                new_input_dir = os.path.join(input_parent, output_subdir)

                # 如果新文件夹名与当前文件夹名相同，跳过重命名
                if os.path.abspath(new_input_dir) == input_dir_abs:
                    log(f"ℹ️  输入文件夹名称已符合规范，无需重命名")
                # 如果新文件夹已存在，跳过重命名并给出警告
                elif os.path.exists(new_input_dir):
                    log(f"⚠️  目标文件夹已存在: {new_input_dir}，跳过重命名", "WARN")
                else:
                    shutil.move(input_dir_abs, new_input_dir)
                    log(f"✅ 输入文件夹已重命名为: {output_subdir}")
            except Exception as e:
                log(f"⚠️  重命名输入文件夹失败: {e}，文件夹保持原名称", "WARN")

    finally:
        # 兜底关闭线程池（异常退出时可能尚未 shutdown）
        if text_executor is not None:
            try:
                text_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        if image_executor is not None:
            try:
                image_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        # 清理临时 ZIP 文件
        if zip_path and os.path.exists(zip_path):
            try:
                os.remove(zip_path)
                log(f"✅ 已清理临时文件: {zip_path}")
            except Exception as e:
                log(f"⚠️  清理临时文件失败: {e}", "WARN")
        # 清理 TeX 转换产生的临时工作目录
        if is_tex_input and tex_work_dir and os.path.isdir(tex_work_dir):
            if os.getenv("BLABLA_KEEP_TEX_BUILD", "").lower() in ("1", "true", "yes"):
                log(f"ℹ️  保留 TeX 临时目录（BLABLA_KEEP_TEX_BUILD=1）: {tex_work_dir}")
            else:
                try:
                    shutil.rmtree(tex_work_dir, ignore_errors=True)
                except Exception as e:
                    log(f"⚠️  清理 TeX 临时目录失败: {e}", "WARN")


if __name__ == "__main__":
    main()
