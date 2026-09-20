"""Safe network diagnostics and bounded, durable retry scheduling."""
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random

import httpx

MODEL_WAIT = ContextVar("easyagent_model_wait", default=(1, {}))


class ModelResponseError(RuntimeError):
    def __init__(self, reason=None):
        self.reason = reason if reason in ('max_output_tokens', 'length', 'content_filter',
                                           'server_error', 'rate_limit_exceeded', 'cancelled') else 'unknown'
        self.retryable = self.reason in ('server_error', 'rate_limit_exceeded')
        super().__init__('model response incomplete: ' + self.reason)

    @property
    def output_limited(self):
        return self.reason in ('max_output_tokens', 'length')


def retry_after(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(120, max(0, seconds))


def attempt_number(job):
    return max(1, job["attempts"] - job.get("retry_state", {}).get("base_attempts", 0))


def model_timeout(default):
    attempt, retry = MODEL_WAIT.get()
    # A retry must not turn an unlimited connection into a finite 120/300-second wait.
    if default is None or ('model_timeout' in retry and retry['model_timeout'] is None):
        return None
    return max(default, retry.get('model_timeout') or default * min(2.5, 1 + .5 * (attempt - 1)))


def error_info(exc):
    """Never include upstream bodies, request URLs, headers or credential-bearing messages."""
    kind = type(exc).__name__
    timeout = getattr(exc, "wait_seconds", None)
    model = getattr(exc, "model_alias", None)
    subject = f"模型 {model}" if model else "远程服务"
    status = getattr(exc, "status", None)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    from .model_limits import ContextWindowError
    if isinstance(exc, ContextWindowError):
        category, retryable = 'context_limit', False
        message = f'{subject}上下文已满，无法在保留原始要求的前提下继续压缩；已完成步骤已保存。'
    elif isinstance(exc, ModelResponseError):
        retryable = exc.retryable
        category = 'output_limit' if exc.reason in ('max_output_tokens', 'length') else 'response_incomplete'
        message = (f'{subject}单次输出达到上限，本次残缺结果未执行。已完成步骤已保留。'
                   if category == 'output_limit' else f'{subject}已响应，但未返回完整结果。'
                   + ('服务暂时异常，将按重试策略处理。' if retryable else '请检查模型服务或调整任务后重试。'))
    elif isinstance(exc, httpx.ReadTimeout):
        category, retryable = "read_timeout", True
        message = f"{subject}响应超时" + (f"（等待 {timeout:g} 秒未收到数据）" if timeout else "（等待返回数据超过时限）")
    elif isinstance(exc, httpx.TimeoutException):
        category, retryable = "network_timeout", True
        message = f"连接或传输到{subject}超时，请检查网络或稍后重试。"
    elif isinstance(exc, httpx.LocalProtocolError):
        category, retryable = "configuration", False
        message = "请求配置不符合网络协议，请检查参数和请求头。"
    elif isinstance(exc, httpx.TransportError):
        category, retryable = "network", True
        message = f"与{subject}的网络连接中断，可稍后重试。"
    elif isinstance(status, int):
        retryable = status in (408, 429) or 500 <= status <= 599
        category = "rate_limit" if status == 429 else "service_unavailable" if retryable else "configuration"
        message = (f"{subject}请求过多（HTTP 429），等待后重试。" if status == 429 else
                   f"{subject}暂时不可用（HTTP {status}），可稍后重试。" if retryable else
                   f"{subject}拒绝请求（HTTP {status}），请检查凭证、模型权限和请求配置。")
    elif isinstance(exc, TimeoutError):
        category, retryable = "step_timeout", True
        message = "此步骤超过执行时限，已停止本次尝试。"
    elif isinstance(exc, ValueError) and str(exc).startswith('run budget exceeded: '):
        category, retryable = 'budget', False
        name = str(exc).removeprefix('run budget exceeded: ')
        label = {'model_calls': '模型调用次数', 'output_reserved': '总输出额度',
                 'tool_calls': '工具调用次数', 'child_runs': '子流程次数'}.get(name, '运行额度')
        message = f'任务的{label}已用尽，当前草稿和已完成步骤已保存。'
    else:
        category, retryable = "execution", False
        message = f"步骤执行失败（{kind}），请查看错误详情。"
    return {"category": category, "retryable": retryable, "message": message,
            "response_reason": getattr(exc, 'reason', None),
            "error_type": kind, "model": model, "timeout_seconds": timeout,
            "retry_after": (retry_after(exc.response.headers.get('retry-after')) if isinstance(exc, httpx.HTTPStatusError)
                            else getattr(exc, "retry_after", None))}


def retry_delay(info, attempt):
    base = min(30, (2 if info["retryable"] else .2) * 2 ** min(attempt - 1, 8))
    return max(base + random.uniform(0, base * .2), info.get("retry_after") or 0)
