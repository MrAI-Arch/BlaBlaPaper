"""
轻量分级日志。

- stdout 按 verbosity 门槛过滤（默认 INFO）。
- 始终追加到日志文件（带时间戳），文件捕获 DEBUG 级全量。
- 线程安全（LLM 调用与逐图分析在 ThreadPoolExecutor 里并发打印）。
- set_log_file 调用前的早期日志先缓冲，调用时刷盘——保证输入检测 /
  MinerU / TeX 转换等 OUTPUT_DIR 确定前的日志也进文件。
- progress() 专给 `\r` 原地进度：只上 stdout，不进文件，避免污染日志。

不用 Python `logging`：现有 print 格式多样（emoji、`\r`、`end=""`），
`logging` 迁移会破坏且过度工程；这里显式处理 `\r`，改动最小。
"""
import sys
import time
import threading

_LEVELS = {"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3}
_DEFAULT = 2  # INFO

_verbosity = _DEFAULT
_log_file = None
_buffer = []
_lock = threading.Lock()
_bars_active = False


def set_verbosity(level):
    """设 stdout 门槛：'ERROR' / 'WARN' / 'INFO' / 'DEBUG'。"""
    global _verbosity
    _verbosity = _LEVELS.get(str(level).upper(), _DEFAULT)


def set_bars_active(active):
    """tqdm 进度条活跃时设 True：log() 的 stdout 被抑制（只进文件），
    write() 仍用 tqdm.write 输出里程碑。"""
    global _bars_active
    _bars_active = active


def set_log_file(path):
    """打开日志文件并刷入早期缓冲；之后 log() 实时追加。"""
    global _log_file
    with _lock:
        if _log_file is not None:
            _log_file.close()
        _log_file = open(path, "a", encoding="utf-8")
        if _buffer:
            _log_file.writelines(_buffer)
            _buffer.clear()
        _log_file.flush()


def log(msg, level="INFO", stage=None):
    """level <= stdout 门槛则打 stdout；始终追加到日志文件（带时间戳）。
    进度条活跃时（_bars_active=True）：stdout 被抑制，仅 WARN/ERROR
    经 tqdm.write 输出以避免撕裂进度条。"""
    lv = _LEVELS.get(str(level).upper(), _DEFAULT)
    with _lock:
        if lv <= _verbosity and not _bars_active:
            print(msg, flush=True)
        file_line = f"[{time.strftime('%H:%M:%S')}] {msg}\n" if stage is None else f"[{time.strftime('%H:%M:%S')}] [{stage}] {msg}\n"
        if _log_file is not None:
            _log_file.write(file_line)
            _log_file.flush()
        else:
            _buffer.append(file_line)
    # 进度条活跃时：WARN/ERROR 仍需上 stdout（用 tqdm.write 避免撕裂）
    if _bars_active and lv <= _verbosity and lv <= _LEVELS["WARN"]:
        try:
            import tqdm as _tqdm
            _tqdm.tqdm.write(msg)
        except ImportError:
            pass


def progress(msg):
    """`\r` 原地进度：只写 stdout，不进文件。msg 需自带前导 \\r。"""
    with _lock:
        sys.stdout.write(msg)
        sys.stdout.flush()


def write(msg, level="INFO"):
    """tqdm.write 封装：在多行进度条不撕裂的前提下打印一行文本（同时进文件）。
    用于里程碑日志（✅/⚠️/❌）穿插在 tqdm bar 之间。"""
    lv = _LEVELS.get(str(level).upper(), _DEFAULT)
    with _lock:
        file_line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
        if _log_file is not None:
            _log_file.write(file_line)
            _log_file.flush()
        else:
            _buffer.append(file_line)
    if lv <= _verbosity:
        try:
            import tqdm as _tqdm
            _tqdm.tqdm.write(msg)
        except ImportError:
            print(msg, flush=True)
