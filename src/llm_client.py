"""
LLM API 客户端模块 - 封装 OpenAI-compatible API 调用
"""
import requests
import time
import json
import copy
import threading
from urllib.parse import urlparse

from . import config
from . import logutil
from .utils import clean_llm_output


_TOTAL_COUNT = 0
_FAILURE_COUNT = 0
_COUNTER_LOCK = threading.Lock()


def _next_request_id():
    global _TOTAL_COUNT
    with _COUNTER_LOCK:
        _TOTAL_COUNT += 1
        return _TOTAL_COUNT


def _record_failure():
    global _FAILURE_COUNT
    with _COUNTER_LOCK:
        _FAILURE_COUNT += 1


def stats():
    """返回 (总调用数, 失败数)，供运行摘要使用。"""
    with _COUNTER_LOCK:
        return _TOTAL_COUNT, _FAILURE_COUNT


def _api_path(api_url):
    parsed = urlparse(api_url)
    return parsed.path or api_url


def _summarize_content(content):
    text_chars = 0
    images = 0
    if isinstance(content, str):
        return len(content), 0
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in {"text", "input_text"}:
                text_chars += len(str(part.get("text", "")))
            elif part_type in {"image_url", "input_image"}:
                images += 1
            elif "text" in part:
                text_chars += len(str(part.get("text", "")))
    elif content is not None:
        text_chars += len(str(content))
    return text_chars, images


def _summarize_messages(messages, new_query):
    text_chars = 0
    images = 0
    for message in messages:
        t, i = _summarize_content(message.get("content", ""))
        text_chars += t
        images += i
    t, i = _summarize_content(new_query)
    text_chars += t
    images += i
    return text_chars, images


def _response_excerpt(resp, limit=400):
    try:
        body = resp.text or ""
    except Exception:
        return ""
    body = " ".join(body.split())
    if len(body) > limit:
        return body[:limit] + "..."
    return body


def _strip_cache_control(value):
    if isinstance(value, dict):
        return {
            key: _strip_cache_control(val)
            for key, val in value.items()
            if key != "cache_control"
        }
    if isinstance(value, list):
        return [_strip_cache_control(item) for item in value]
    return value


def _content_to_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                chunks.append(part.get("text", ""))
        return "\n".join(chunk for chunk in chunks if chunk)
    return str(content) if content is not None else ""


def _to_responses_content(content):
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]

    if not isinstance(content, list):
        return [{"type": "input_text", "text": str(content)}] if content is not None else []

    items = []
    for part in content:
        if not isinstance(part, dict):
            continue

        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text", "")
            if text:
                items.append({"type": "input_text", "text": text})
        elif part_type == "image_url":
            image = part.get("image_url", {})
            image_url = image.get("url") if isinstance(image, dict) else image
            if image_url:
                item = {"type": "input_image", "image_url": image_url}
                if isinstance(image, dict) and image.get("detail"):
                    item["detail"] = image["detail"]
                items.append(item)
        elif part_type in {"input_text", "input_image"}:
            items.append(_strip_cache_control(part))
        elif "text" in part:
            text = str(part.get("text", ""))
            if text:
                items.append({"type": "input_text", "text": text})

    return items


def _build_responses_payload(messages, model_name, json_mode):
    instructions = []
    input_items = []

    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")

        if role == "system":
            text = _content_to_text(content)
            if text:
                instructions.append(text)
            continue

        if role == "assistant":
            continue

        response_role = role if role in {"user", "developer"} else "user"
        response_content = _to_responses_content(content)
        if response_content:
            input_items.append({"role": response_role, "content": response_content})

    payload = {
        "model": model_name,
        "input": input_items,
        "temperature": 0.3,
        "store": False,
    }
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)
    if json_mode:
        payload["text"] = {"format": {"type": "json_object"}}
    return payload


def _build_chat_payload(messages, model_name, json_mode):
    payload = {
        "model": model_name,
        "messages": _strip_cache_control(messages),
        "temperature": 0.3,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _extract_responses_text(data):
    if isinstance(data.get("output_text"), str):
        return data["output_text"]

    chunks = []
    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content", []):
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
        elif item.get("type") in {"output_text", "text"} and isinstance(item.get("text"), str):
            chunks.append(item["text"])

    return "".join(chunks).strip() if chunks else None


def _extract_chat_text(data):
    content = data["choices"][0]["message"]["content"]
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        return "".join(chunks).strip()
    return str(content)


def call_llm_with_cache(messages, new_query, api_key, api_url, model_name, json_mode=False, stage_name="LLM", wire_api=None, strip_headings=True):
    """
    调用 LLM API，支持 Responses API 与 OpenAI-compatible Chat Completions。

    Args:
        messages: 基础消息列表（包含上下文）
        new_query: 新的查询文本（将添加到消息末尾）
        api_key: API 密钥
        api_url: API 端点 URL
        model_name: 使用的模型名称
        json_mode: 是否强制 JSON 结构化输出（默认 False）

    Returns:
        LLM 响应文本（json_mode=True 时返回原始 JSON，否则返回清理后的文本）
        失败时返回 None
    """
    current_messages = copy.deepcopy(messages)

    if new_query:
        current_messages.append({"role": "user", "content": new_query})

    effective_wire = wire_api if wire_api is not None else config.WIRE_API
    use_responses = effective_wire == "responses"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    request_id = _next_request_id()
    text_chars, image_count = _summarize_messages(messages, new_query)
    started = time.monotonic()
    logutil.log(
        f"[LLM:{request_id}] start stage={stage_name} model={model_name} "
        f"api={effective_wire} path={_api_path(api_url)} json={json_mode} "
        f"text_chars={text_chars} images={image_count}",
        "DEBUG",
    )

    for attempt in range(config.LLM_MAX_RETRIES):
        try:
            if use_responses:
                payload = _build_responses_payload(current_messages, model_name, json_mode)
            else:
                payload = _build_chat_payload(current_messages, model_name, json_mode)

            resp = requests.post(
                api_url,
                json=payload,
                headers=headers,
                timeout=(config.LLM_CONNECT_TIMEOUT, config.LLM_READ_TIMEOUT),
            )

            if resp.status_code == 429:
                wait_time = 2 * (2 ** attempt)
                logutil.log(f"[LLM:{request_id}] rate_limited stage={stage_name} attempt={attempt + 1} wait={wait_time}s", "WARN")
                time.sleep(wait_time)
                continue

            if resp.status_code >= 400:
                logutil.log(
                    f"[LLM:{request_id}] http_error stage={stage_name} attempt={attempt + 1} "
                    f"status={resp.status_code} model={model_name} api={effective_wire} "
                    f"path={_api_path(api_url)} text_chars={text_chars} body={_response_excerpt(resp)}",
                    "ERROR",
                )
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, dict) and data.get("error"):
                logutil.log(f"[LLM:{request_id}] api_error stage={stage_name} model={model_name} error={data['error']}", "ERROR")
                _record_failure()
                return None

            content = _extract_responses_text(data) if use_responses else _extract_chat_text(data)
            elapsed = time.monotonic() - started
            output_chars = len(content or "")
            logutil.log(
                f"[LLM:{request_id}] ✓ {stage_name} {elapsed:.1f}s → {output_chars}c (attempt {attempt + 1})",
                "DEBUG",
            )
            return content if json_mode else clean_llm_output(content, strip_headings=strip_headings)

        except requests.exceptions.RequestException as e:
            elapsed = time.monotonic() - started
            logutil.log(
                f"[LLM:{request_id}] request_error stage={stage_name} attempt={attempt + 1}/"
                f"{config.LLM_MAX_RETRIES} elapsed={elapsed:.1f}s model={model_name} "
                f"api={effective_wire} path={_api_path(api_url)} error={e}",
                "ERROR",
            )
            if attempt < config.LLM_MAX_RETRIES - 1:
                time.sleep(2)

        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            elapsed = time.monotonic() - started
            logutil.log(
                f"[LLM:{request_id}] response_error stage={stage_name} attempt={attempt + 1}/"
                f"{config.LLM_MAX_RETRIES} elapsed={elapsed:.1f}s model={model_name} error={e}",
                "ERROR",
            )
            if attempt < config.LLM_MAX_RETRIES - 1:
                time.sleep(2)

        except Exception as e:
            elapsed = time.monotonic() - started
            logutil.log(
                f"[LLM:{request_id}] unexpected_error stage={stage_name} attempt={attempt + 1}/"
                f"{config.LLM_MAX_RETRIES} elapsed={elapsed:.1f}s model={model_name} error={e}",
                "ERROR",
            )
            if attempt < config.LLM_MAX_RETRIES - 1:
                time.sleep(2)

    elapsed = time.monotonic() - started
    logutil.log(
        f"[LLM:{request_id}] failed stage={stage_name} model={model_name} "
        f"api={effective_wire} path={_api_path(api_url)} elapsed={elapsed:.1f}s",
        "ERROR",
    )
    _record_failure()
    return None
