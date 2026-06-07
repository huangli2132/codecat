"""模型后端适配层。

runtime 只关心一件事：给我一个 prompt，我拿回一段文本。
不同 provider 在 HTTP 接口、响应结构、是否支持 prompt cache 上都有差异，
这些差异都在这里被抹平成统一的 complete() 接口。
"""

import json
import socket
import time
from http.client import RemoteDisconnected
import urllib.error
import urllib.request

from .errors import ProviderError, sanitize_url

OPENAI_COMPATIBLE_USER_AGENT = "codecat/0.3"
RETRYABLE_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _extract_openai_text(data):
    if data.get("output_text"):
        return data["output_text"]

    for item in data.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict):
                text = content.get("text")
                if text:
                    return text

    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        return text

    return ""


def _extract_openai_text_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
            continue
        if event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text
        part = event.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                return text
        item = event.get("item")
        if isinstance(item, dict):
            text = _extract_openai_text({"output": [item]})
            if text:
                return text
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            text = _extract_openai_text(response)
            if text:
                return text
        text = _extract_openai_text(event)
        if text:
            return text
    if deltas:
        return "".join(deltas)
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response)
    return ""


def _extract_openai_response_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            if event.get("type") == "response.completed":
                text = _extract_openai_text(response)
                if text:
                    return text, response
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text, last_response or {}
        else:
            text = _extract_openai_text(event)
            if text:
                return text, event
    if deltas:
        return "".join(deltas), last_response or {}
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response), last_response
    return "", {}


def _extract_usage_cache_details(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，
    # 让 runtime/trace/report 不需要关心 provider 细节。
    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


def _request_with_retries(provider, model, base_url, request, timeout, retry_budget=2):
    retry_count = 0
    attempts = int(retry_budget) + 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body_text = response.read().decode("utf-8")
                headers = getattr(response, "headers", {}) or {}
                content_type = headers.get("Content-Type", "")
            return body_text, content_type, _provider_metadata(provider, model, base_url, attempt + 1, retry_count)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code in RETRYABLE_HTTP_STATUS or exc.code >= 500
            if retryable and attempt < attempts - 1:
                retry_count += 1
                time.sleep(_retry_delay(attempt, exc.headers))
                continue
            raise ProviderError(
                f"{provider} provider request failed with HTTP {exc.code}",
                provider=provider,
                model=model,
                base_url=base_url,
                code=_http_error_code(exc.code),
                http_status=exc.code,
                retryable=retryable,
                attempts=attempt + 1,
                retry_count=retry_count,
                body_excerpt=body,
                cause_type=type(exc).__name__,
            ) from exc
        except (urllib.error.URLError, RemoteDisconnected, TimeoutError, socket.timeout) as exc:
            retryable = True
            if attempt < attempts - 1:
                retry_count += 1
                time.sleep(_retry_delay(attempt, None))
                continue
            raise ProviderError(
                f"{provider} provider request failed before a valid response",
                provider=provider,
                model=model,
                base_url=base_url,
                code=_transport_error_code(exc),
                retryable=retryable,
                attempts=attempt + 1,
                retry_count=retry_count,
                cause_type=type(exc).__name__,
            ) from exc
    raise AssertionError("unreachable provider retry loop")


def _provider_metadata(provider, model, base_url, attempts, retry_count):
    return {
        "provider_protocol": provider,
        "provider_model": model,
        "provider_base_url": sanitize_url(base_url),
        "provider_attempts": int(attempts),
        "provider_retry_count": int(retry_count),
    }


def _http_error_code(status):
    status = int(status)
    if status == 401 or status == 403:
        return "auth_error"
    if status == 408:
        return "timeout"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    return "http_error"


def _transport_error_code(exc):
    reason = getattr(exc, "reason", None)
    text = f"{exc} {reason}".lower()
    if isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in text:
        return "timeout"
    return "network_error"


def _retry_delay(attempt, headers):
    retry_after = _retry_after_seconds(headers)
    if retry_after is not None:
        return min(retry_after, 2.0)
    return 0.5 * (attempt + 1)


def _retry_after_seconds(headers):
    if not headers:
        return None
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def _provider_failure(provider, model, base_url, code, message, request_metadata=None, cause=None):
    request_metadata = request_metadata or {}
    error = ProviderError(
        message,
        provider=provider,
        model=model,
        base_url=base_url,
        code=code,
        retryable=False,
        attempts=request_metadata.get("provider_attempts", 1),
        retry_count=request_metadata.get("provider_retry_count", 0),
        cause_type=type(cause).__name__ if cause else "",
    )
    return error


class OpenAICompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        # 当前只在明确支持 prompt cache 语义的后端上启用这条链路，
        # 避免对不支持的后端传一个“看起来统一、其实没意义”的伪参数。
        self.supports_prompt_cache = any(host in self.base_url for host in ("openai.com", "right.codes"))
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        """向 OpenAI-compatible `/responses` 接口发起一次模型调用。

        为什么存在：
        runtime 不应该知道 HTTP 细节、SSE 细节、usage 字段长什么样，
        更不应该自己去判断 prompt cache 参数要不要带。这个函数把这些后端
        细节都包起来，对上层暴露统一的 `complete()` 行为。

        输入 / 输出：
        - 输入：完整 prompt、最大输出 token，以及可选的 prompt cache 参数
        - 输出：模型最终文本；同时把 usage / cached_tokens 等元数据写进
          `self.last_completion_metadata`

        在 agent 链路里的位置：
        它位于 `Pico.ask()` 的模型调用阶段，是稳定前缀缓存复用链路真正
        落到 provider API 的地方。
        """
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_output_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        # runtime 传入的是“稳定前缀”的签名，而不是整段 prompt 的签名。
        # 这样缓存复用针对的是稳定段，不会因为动态 history 每轮变化而失效。
        if self.supports_prompt_cache and prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        if self.supports_prompt_cache and prompt_cache_retention:
            payload["prompt_cache_retention"] = prompt_cache_retention

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            body_text, content_type, request_metadata = _request_with_retries(
                "openai",
                self.model,
                self.base_url,
                request,
                self.timeout,
            )
        except ProviderError as exc:
            self.last_completion_metadata = exc.to_metadata()
            raise

        # 有些兼容后端返回普通 JSON，有些返回 SSE。
        # 这里两种都接住，并尽量统一抽取文本和 usage/cache 元数据。
        if content_type.startswith("text/event-stream") or body_text.lstrip().startswith("data:"):
            text, response_data = _extract_openai_response_from_sse(body_text)
            if isinstance(response_data, dict) and response_data:
                # 这些元数据会一路传回 runtime，进入 trace 和 report，
                # 用来观察 prompt cache 是否真的命中。
                self.last_completion_metadata = {
                    "prompt_cache_supported": self.supports_prompt_cache,
                    "prompt_cache_key": prompt_cache_key,
                    "prompt_cache_retention": prompt_cache_retention,
                    **request_metadata,
                    **_extract_usage_cache_details(response_data),
                }
            if text:
                return text
            error = _provider_failure(
                "openai",
                self.model,
                self.base_url,
                "empty_response",
                "OpenAI-compatible error: could not extract text from event stream response",
                request_metadata,
            )
            self.last_completion_metadata = error.to_metadata()
            raise error

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            error = _provider_failure(
                "openai",
                self.model,
                self.base_url,
                "invalid_json",
                "OpenAI-compatible error: backend returned non-JSON content that could not be parsed",
                request_metadata,
                cause=exc,
            )
            self.last_completion_metadata = error.to_metadata()
            raise error from exc
        if data.get("error"):
            error = _provider_failure(
                "openai",
                self.model,
                self.base_url,
                "provider_error",
                f"OpenAI-compatible error: {data['error']}",
                request_metadata,
            )
            self.last_completion_metadata = error.to_metadata()
            raise error
        self.last_completion_metadata = {
            "prompt_cache_supported": self.supports_prompt_cache,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_retention": prompt_cache_retention,
            **request_metadata,
            **_extract_usage_cache_details(data),
        }
        text = _extract_openai_text(data)
        if text:
            return text
        error = _provider_failure(
            "openai",
            self.model,
            self.base_url,
            "empty_response",
            "OpenAI-compatible error: could not extract text from response",
            request_metadata,
        )
        self.last_completion_metadata = error.to_metadata()
        raise error


def _extract_anthropic_text(data):
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                return text
    return ""


class AnthropicCompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        # 为了保持统一接口，runtime 仍然会传缓存参数进来；
        # 这里只是显式丢弃，因为当前 Anthropic-compatible 路径没有接缓存复用。
        del prompt_cache_key, prompt_cache_retention
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            body_text, _content_type, request_metadata = _request_with_retries(
                "anthropic",
                self.model,
                self.base_url,
                request,
                self.timeout,
            )
        except ProviderError as exc:
            self.last_completion_metadata = exc.to_metadata()
            raise

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            error = _provider_failure(
                "anthropic",
                self.model,
                self.base_url,
                "invalid_json",
                "Anthropic-compatible error: backend returned non-JSON content that could not be parsed",
                request_metadata,
                cause=exc,
            )
            self.last_completion_metadata = error.to_metadata()
            raise error from exc
        if data.get("error"):
            error = _provider_failure(
                "anthropic",
                self.model,
                self.base_url,
                "provider_error",
                f"Anthropic-compatible error: {data['error']}",
                request_metadata,
            )
            self.last_completion_metadata = error.to_metadata()
            raise error
        text = _extract_anthropic_text(data)
        if text:
            self.last_completion_metadata = dict(request_metadata)
            return text
        error = _provider_failure(
            "anthropic",
            self.model,
            self.base_url,
            "empty_response",
            "Anthropic-compatible error: could not extract text from response",
            request_metadata,
        )
        self.last_completion_metadata = error.to_metadata()
        raise error
