"""
配置模块 - 从项目 .env 和环境变量中加载配置
"""
import os
from pathlib import Path

from . import logutil

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(dotenv_path=None):
        """轻量级 .env 回退实现，避免运行时必须安装 python-dotenv。"""
        path = Path(dotenv_path) if dotenv_path is not None else Path.cwd() / ".env"
        if not path.exists():
            return False

        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return True


# 加载项目根目录的 .env。这样即使从其他目录执行 main.py，也能读到配置。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv()


def _env(*names, default=""):
    """按顺序读取环境变量，支持小写 .env 字段和常见大写别名。
    优先使用 .env 文件中的值（通过 dotenv 加载），再回退到系统环境变量。"""
    for name in names:
        value = _dotenv_values.get(name) or os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


# 预读取 .env 以便 _env 优先使用 .env 中的值
_dotenv_values = {}
_dotenv_path = Path.cwd() / ".env"
if (PROJECT_ROOT / ".env").exists():
    _dotenv_path = PROJECT_ROOT / ".env"
try:
    for raw_line in _dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        _dotenv_values[key.strip()] = value.strip().strip('"').strip("'")
except Exception:
    pass


def _normalize_wire_api(value):
    normalized = value.strip().replace("-", "_").lower()
    if normalized in {"response", "responses_api"}:
        return "responses"
    if normalized in {"chat", "chat_completions", "chat/completions"}:
        return "chat_completions"
    return normalized or "responses"


def _env_int(*names, default):
    value = _env(*names, default=str(default))
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_api_url(base_url, wire_api):
    base = base_url.rstrip("/")
    if base.endswith("/responses") or base.endswith("/chat/completions"):
        return base
    if wire_api == "responses":
        return f"{base}/responses"
    return f"{base}/chat/completions"


# ============================================================================
# LLM API 配置
# ============================================================================

MODEL_PROVIDER = _env("model_provider", "MODEL_PROVIDER", default="OpenAI")

# 图片（多模态）模型必填：纯文本模型无法处理图片，禁止回退到 text 模型。
MODEL_NAME_IMAGE = _env("model_image", "MODEL_NAME_IMAGE")
if not MODEL_NAME_IMAGE:
    raise ValueError(
        "model_image 未设置：逐图分析需要视觉能力，请在 .env 配置 model_image "
        "为一个多模态模型（如 glm-4v）。"
    )

# 文本模型可回退到图片模型（多模态也能做纯文本），但单价通常更高，需提示。
_model_text_explicit = bool(_env("model", "MODEL", "OPENAI_MODEL"))
MODEL_NAME_TEXT = _env("model", "MODEL", "OPENAI_MODEL", default=MODEL_NAME_IMAGE)
if not _model_text_explicit:
    logutil.log(
        f"[提示] 未配置 model，文本分析将复用图片模型 {MODEL_NAME_IMAGE!r}"
        "（多模态，单价通常更高）。如需降低成本，请在 .env 设置 model 为纯文本模型"
        "（如 deepseek-chat / glm-4-flash）。",
        "WARN",
    )
API_BASE_URL = _env(
    "base_url",
    "BASE_URL",
    "OPENAI_BASE_URL",
    default="https://api.openai.com/v1",
)
WIRE_API = _normalize_wire_api(
    _env("wire_api", "WIRE_API", "OPENAI_WIRE_API", default="responses")
)
API_URL = _env("api_url", "API_URL", "OPENAI_API_URL") or _build_api_url(
    API_BASE_URL,
    WIRE_API,
)
API_KEY = _env("OPENAI_API_KEY", "openai_api_key")

# ---- 图片分析专用供应商（text+picture）----
# 未配置时回退到上面的 text 供应商，逐图分析行为与原先一致。
# 配置后，analyze_single_figure_isolated 将走此供应商。
IMAGE_API_KEY = _env("IMAGE_API_KEY", "image_api_key", default=API_KEY)
IMAGE_WIRE_API = _normalize_wire_api(
    _env("image_wire_api", "IMAGE_WIRE_API", default=WIRE_API)
)
IMAGE_BASE_URL = _env(
    "image_base_url",
    "IMAGE_BASE_URL",
    default=API_BASE_URL,
)
IMAGE_API_URL = _env("image_api_url", "IMAGE_API_URL") or _build_api_url(
    IMAGE_BASE_URL,
    IMAGE_WIRE_API,
)

MODEL_REASONING_EFFORT = _env("MODEL_REASONING_EFFORT", default="medium")
LLM_CONNECT_TIMEOUT = _env_int("LLM_CONNECT_TIMEOUT", default=20)
LLM_READ_TIMEOUT = _env_int("LLM_READ_TIMEOUT", default=300)
LLM_MAX_RETRIES = _env_int("LLM_MAX_RETRIES", default=3)
TEXT_MAX_WORKERS = max(1, _env_int("TEXT_MAX_WORKERS", default=2))
IMAGE_MAX_WORKERS = max(1, _env_int("IMAGE_MAX_WORKERS", default=2))

if not API_KEY:
    raise ValueError(
        "OPENAI_API_KEY not found. Please set it in the project .env file "
        "or in your shell environment."
    )

# ============================================================================
# 系统提示词
# ============================================================================

UNIFIED_SYSTEM_PROMPT = (
    "你是一个专业的计算机科学与人工智能论文分析专家。"
    "请根据提供的论文内容和图表进行深入分析。"
)

# ============================================================================
# MinerU PDF 解析服务配置
# ============================================================================

MINERU_API_TOKEN = _env("MINERU_API_TOKEN")

if not MINERU_API_TOKEN:
    logutil.log("[警告] MINERU_API_TOKEN 未配置，PDF 解析功能将不可用", "WARN")

MINERU_BASE_URL = "https://mineru.net/api/v4"
MINERU_MODEL_VERSION = "pipeline"  # 可选: "pipeline" 或 "vlm"
