"""Provider client unit tests.

用 mock HTTP 覆盖 OpenAI-compatible 和 Anthropic-compatible 两套客户端的
正常/异常路径：JSON 响应、SSE 响应、HTTP 错误、重试、usage 提取、cache 元数据。
"""

import io
import json
import socket
import urllib.error
import urllib.request
from http.client import RemoteDisconnected

import pytest

from codecat.providers import AnthropicCompatibleModelClient, OpenAICompatibleModelClient
from codecat.providers.errors import ProviderError


# ── helpers ───────────────────────────────────────────────────


class _FakeResponse(io.BytesIO):
    """mock urlopen 的返回值，同时充当可读流和带 headers 的响应对象。"""

    def __init__(self, body_bytes, status=200, content_type="application/json", headers=None):
        super().__init__(body_bytes)
        self.status = status
        self._content_type = content_type
        self._headers = headers or {}
        self._headers.setdefault("Content-Type", content_type)
        self.headers = self._headers

    def read(self, *args):
        return super().read(*args)

    def getcode(self):
        return self.status


def _make_http_error(status, body_str, headers=None, url="https://api.openai.com/v1/responses"):
    """构建 urllib.error.HTTPError 实例用于测试。"""
    from io import BytesIO
    hdrs = headers or {}
    fp = BytesIO(body_str.encode("utf-8"))
    # HTTPError(url, code, msg, hdrs, fp)
    return urllib.error.HTTPError(str(url), status, "HTTP Error", hdrs, fp)


def _make_fake_urlopen(returns, *, raises=None):
    """创建 mock urlopen，按顺序返回 _FakeResponse 或触发异常。

    *raises* 如果是列表则按序 pop；也可以是单个异常对象。
    如果 *returns* 中的响应 status >= 400，自动转为 HTTPError 抛出
    （模拟真实 urlopen 行为）。
    """

    def fake_urlopen(request, timeout=None):
        if raises is not None:
            if isinstance(raises, list):
                if raises:
                    exc = raises.pop(0)
                    raise exc
            else:
                raise raises
        if not returns:
            raise RuntimeError("mock urlopen ran out of responses")
        resp = returns.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        # 模拟 urlopen 的真实行为：非 2xx status 自动抛 HTTPError
        if hasattr(resp, 'status') and resp.status >= 400:
            body = resp.read().decode("utf-8", errors="replace") if hasattr(resp, 'read') else ""
            raise _make_http_error(resp.status, body, getattr(resp, 'headers', None))
        return resp

    return fake_urlopen


def _body(payload_dict) -> bytes:
    return json.dumps(payload_dict).encode("utf-8")


def _sse_body(lines):
    """构建 SSE 文本的字节表示。"""
    text = "\n".join(lines)
    return text.encode("utf-8")


# ── OpenAI-compatible 正常路径 ────────────────────────────────


class TestOpenAICompatibleNormal:
    def test_basic_json_response(self, monkeypatch):
        """JSON 响应能正确提取 text 和 usage 元数据。"""
        returns = [
            _FakeResponse(
                _body({
                    "output_text": "hello world",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 3,
                        "total_tokens": 13,
                    },
                }),
                content_type="application/json",
            )
        ]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))

        client = OpenAICompatibleModelClient(
            model="gpt-5.4",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            temperature=0.7,
            timeout=30,
        )
        result = client.complete("hello", max_new_tokens=100)

        assert result == "hello world"
        meta = client.last_completion_metadata
        assert meta["input_tokens"] == 10
        assert meta["output_tokens"] == 3
        assert meta["total_tokens"] == 13
        assert meta["cache_hit"] is False
        assert meta["cached_tokens"] == 0

    def test_choices_style_response(self, monkeypatch):
        """兼容 `choices[0].message.content` 风格。"""
        returns = [
            _FakeResponse(
                _body({
                    "choices": [
                        {"message": {"content": "from choices"}}
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                }),
                content_type="application/json",
            )
        ]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))

        client = OpenAICompatibleModelClient(
            model="test", base_url="https://test.example.com/v1",
            api_key="k", temperature=0, timeout=10,
        )
        result = client.complete("p", max_new_tokens=50)

        assert result == "from choices"
        meta = client.last_completion_metadata
        assert meta["input_tokens"] == 5
        assert meta["output_tokens"] == 2

    def test_output_content_array_style(self, monkeypatch):
        """兼容 `output[].content[]` 风格。"""
        returns = [
            _FakeResponse(
                _body({
                    "output": [
                        {"content": [{"text": "nested text"}]}
                    ],
                }),
                content_type="application/json",
            )
        ]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))
        client = OpenAICompatibleModelClient(
            model="t", base_url="https://x.example.com/v1",
            api_key="k", temperature=0, timeout=10,
        )
        assert client.complete("p", max_new_tokens=10) == "nested text"

    def test_sse_stream_response(self, monkeypatch):
        """SSE 流式响应能正确拼接 delta 文本。"""
        returns = [
            _FakeResponse(
                _sse_body([
                    "data: {\"type\":\"response.output_text.delta\",\"delta\":\"Hello \"}",
                    "data: {\"type\":\"response.output_text.delta\",\"delta\":\"World\"}",
                    "data: {\"type\":\"response.output_text.done\",\"text\":\"Hello World\"}",
                    "data: [DONE]",
                ]),
                content_type="text/event-stream",
            )
        ]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))
        client = OpenAICompatibleModelClient(
            model="t", base_url="https://x.example.com/v1",
            api_key="k", temperature=0, timeout=10,
        )
        assert client.complete("p", max_new_tokens=10) == "Hello World"

    def test_sse_fallback_to_deltas(self, monkeypatch):
        """SSE 没有 output_text.done 时，fallback 到 delta 拼接。"""
        returns = [
            _FakeResponse(
                _sse_body([
                    "data: {\"type\":\"response.output_text.delta\",\"delta\":\"part1\"}",
                    "data: {\"type\":\"response.output_text.delta\",\"delta\":\"part2\"}",
                    "data: [DONE]",
                ]),
                content_type="text/event-stream",
            )
        ]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))
        client = OpenAICompatibleModelClient(
            model="t", base_url="https://x.example.com/v1",
            api_key="k", temperature=0, timeout=10,
        )
        assert client.complete("p", max_new_tokens=10) == "part1part2"

    def test_prompt_cache_on_supported_host(self, monkeypatch):
        """right.codes / openai.com 域名自动启用 prompt cache。"""
        returns = [
            _FakeResponse(
                _body({
                    "output_text": "cached response",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 5,
                        "input_tokens_details": {"cached_tokens": 80},
                    },
                }),
                content_type="application/json",
            )
        ]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))

        client = OpenAICompatibleModelClient(
            model="t", base_url="https://api.openai.com/v1",
            api_key="k", temperature=0, timeout=10,
        )
        assert client.supports_prompt_cache is True

        result = client.complete(
            "prefix...question",
            max_new_tokens=50,
            prompt_cache_key="abc123",
            prompt_cache_retention="24h",
        )
        assert result == "cached response"
        meta = client.last_completion_metadata
        assert meta["cache_hit"] is True
        assert meta["cached_tokens"] == 80
        assert meta["prompt_cache_key"] == "abc123"


# ── OpenAI-compatible 错误路径 ────────────────────────────────


class TestOpenAICompatibleErrors:
    def test_http_500_with_retry_exhaustion(self, monkeypatch):
        """HTTP 500 可重试，但重试耗尽后抛 ProviderError。"""
        err_500 = _make_http_error(500, '{"error":"server error"}')
        monkeypatch.setattr(
            urllib.request, "urlopen",
            _make_fake_urlopen([], raises=[err_500, err_500, err_500]),
        )

        client = OpenAICompatibleModelClient(
            model="t", base_url="https://api.openai.com/v1",
            api_key="k", temperature=0, timeout=3,
        )
        with pytest.raises(ProviderError) as exc_info:
            client.complete("p", max_new_tokens=10)

        err = exc_info.value
        assert err.code == "server_error"
        assert err.retryable is True
        assert err.http_status == 500
        assert err.attempts == 3  # 1 initial + 2 retries
        assert err.retry_count == 2

    def test_http_401_is_not_retried(self, monkeypatch):
        """HTTP 401 认证错误不重试，直接抛。"""
        err_401 = _make_http_error(401, '{"error":"unauthorized"}')
        monkeypatch.setattr(
            urllib.request, "urlopen",
            _make_fake_urlopen([], raises=[err_401]),
        )

        client = OpenAICompatibleModelClient(
            model="t", base_url="https://api.openai.com/v1",
            api_key="bad-key", temperature=0, timeout=3,
        )
        with pytest.raises(ProviderError) as exc_info:
            client.complete("p", max_new_tokens=10)

        err = exc_info.value
        assert err.code == "auth_error"
        assert err.retryable is False
        assert err.http_status == 401
        assert err.attempts == 1  # no retry

    def test_transport_error_retry_then_fail(self, monkeypatch):
        """传输错误（网络不通）重试后仍抛 ProviderError。"""
        url_errs = [urllib.error.URLError("connection refused") for _ in range(3)]
        monkeypatch.setattr(
            urllib.request, "urlopen",
            _make_fake_urlopen([], raises=url_errs),
        )

        client = OpenAICompatibleModelClient(
            model="t", base_url="https://api.openai.com/v1",
            api_key="k", temperature=0, timeout=3,
        )
        with pytest.raises(ProviderError) as exc_info:
            client.complete("p", max_new_tokens=10)

        err = exc_info.value
        assert err.code == "network_error"
        assert err.retryable is True
        assert err.attempts == 3

    def test_timeout_transport_error(self, monkeypatch):
        """超时归类为 timeout code。"""
        timeouts = [TimeoutError("timed out") for _ in range(3)]
        monkeypatch.setattr(
            urllib.request, "urlopen",
            _make_fake_urlopen([], raises=timeouts),
        )

        client = OpenAICompatibleModelClient(
            model="t", base_url="https://api.openai.com/v1",
            api_key="k", temperature=0, timeout=3,
        )
        with pytest.raises(ProviderError) as exc_info:
            client.complete("p", max_new_tokens=10)

        assert exc_info.value.code == "timeout"

    def test_invalid_json_response(self, monkeypatch):
        """返回非 JSON 内容时抛 invalid_json 错误。"""
        returns = [
            _FakeResponse(b"<html>not json</html>", status=200,
                          content_type="text/html"),
        ]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))

        client = OpenAICompatibleModelClient(
            model="t", base_url="https://api.openai.com/v1",
            api_key="k", temperature=0, timeout=3,
        )
        with pytest.raises(ProviderError) as exc_info:
            client.complete("p", max_new_tokens=10)

        assert exc_info.value.code == "invalid_json"

    def test_empty_text_response(self, monkeypatch):
        """返回合法 JSON 但没有文本可提取时抛 empty_response。"""
        returns = [
            _FakeResponse(_body({"choices": []}), content_type="application/json"),
        ]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))

        client = OpenAICompatibleModelClient(
            model="t", base_url="https://api.openai.com/v1",
            api_key="k", temperature=0, timeout=3,
        )
        with pytest.raises(ProviderError) as exc_info:
            client.complete("p", max_new_tokens=10)

        assert exc_info.value.code == "empty_response"

    def test_provider_error_sets_metadata_on_client(self, monkeypatch):
        """Provider 错误时 client.last_completion_metadata 被正确设置。"""
        # 用 400（非重试状态码）让 HTTP error 一次性走到 ProviderError
        err_400 = _make_http_error(400, "bad request")
        monkeypatch.setattr(
            urllib.request, "urlopen",
            _make_fake_urlopen([], raises=[err_400]),
        )

        client = OpenAICompatibleModelClient(
            model="t", base_url="https://api.openai.com/v1",
            api_key="k", temperature=0, timeout=3,
        )
        try:
            client.complete("p", max_new_tokens=10)
        except ProviderError:
            pass

        meta = client.last_completion_metadata
        assert "provider_error" in meta
        assert meta["provider_error"]["code"] == "http_error"

    def test_retry_after_header_respected(self, monkeypatch):
        """Retry-After header 控制的延迟。第一次 429 → 重试；第二次 200。"""
        err_429 = _make_http_error(429, "rate limit", {"Retry-After": "0.05"})
        success = _FakeResponse(
            _body({"output_text": "ok"}), status=200,
            content_type="application/json",
        )
        monkeypatch.setattr(
            urllib.request, "urlopen",
            _make_fake_urlopen([success], raises=[err_429]),
        )

        client = OpenAICompatibleModelClient(
            model="t", base_url="https://api.openai.com/v1",
            api_key="k", temperature=0, timeout=10,
        )
        result = client.complete("p", max_new_tokens=10)
        assert result == "ok"
        assert client.last_completion_metadata["provider_attempts"] == 2
        assert client.last_completion_metadata["provider_retry_count"] == 1


# ── Anthropic-compatible 正常路径 ─────────────────────────────


class TestAnthropicCompatibleNormal:
    def test_basic_messages_response(self, monkeypatch):
        """标准 Anthropic Messages API 响应。"""
        returns = [
            _FakeResponse(
                _body({
                    "id": "msg_001",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello from Claude"}],
                    "model": "claude-sonnet-4-6",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 15, "output_tokens": 5},
                }),
                content_type="application/json",
            )
        ]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))

        client = AnthropicCompatibleModelClient(
            model="claude-sonnet-4-6",
            base_url="https://api.anthropic.com/v1",
            api_key="sk-ant-test",
            temperature=0.5,
            timeout=30,
        )
        result = client.complete("hello", max_new_tokens=1000)

        assert result == "Hello from Claude"
        meta = client.last_completion_metadata
        assert meta["provider_protocol"] == "anthropic"
        assert meta["provider_model"] == "claude-sonnet-4-6"

    def test_multiple_content_blocks(self, monkeypatch):
        """多 content block 时只取第一个 text 块。"""
        returns = [
            _FakeResponse(
                _body({
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {}},
                        {"type": "text", "text": "the real answer"},
                    ],
                }),
                content_type="application/json",
            )
        ]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))
        client = AnthropicCompatibleModelClient(
            model="t", base_url="https://x.example.com/v1",
            api_key="k", temperature=0, timeout=10,
        )
        # 第一个 block 是 tool_use，跳过；取第二个 text block
        assert client.complete("p", max_new_tokens=10) == "the real answer"

    def test_cache_params_discarded(self, monkeypatch):
        """Anthropic client 当前丢弃缓存参数（不报错）。"""
        returns = [
            _FakeResponse(
                _body({"content": [{"type": "text", "text": "ok"}]}),
                content_type="application/json",
            )
        ]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))
        client = AnthropicCompatibleModelClient(
            model="t", base_url="https://x.example.com/v1",
            api_key="k", temperature=0, timeout=10,
        )
        # 这些参数应被静默丢弃
        result = client.complete("p", max_new_tokens=10,
                                 prompt_cache_key="ignored",
                                 prompt_cache_retention="ignored")
        assert result == "ok"


# ── Anthropic-compatible 错误路径 ─────────────────────────────


class TestAnthropicCompatibleErrors:
    def test_api_error_response(self, monkeypatch):
        """Anthropic 格式的 error 返回（HTTP 200 + error body）。"""
        returns = [
            _FakeResponse(
                _body({
                    "error": {
                        "type": "invalid_request_error",
                        "message": "model not found",
                    }
                }),
                status=200,
                content_type="application/json",
            )
        ]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))

        client = AnthropicCompatibleModelClient(
            model="bad-model", base_url="https://x.example.com/v1",
            api_key="k", temperature=0, timeout=3,
        )
        with pytest.raises(ProviderError) as exc_info:
            client.complete("p", max_new_tokens=10)

        err = exc_info.value
        assert err.code == "provider_error"

    def test_invalid_json(self, monkeypatch):
        """非 JSON 响应。"""
        returns = [_FakeResponse(b"Internal Server Error", status=200,
                                 content_type="text/plain")]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))

        client = AnthropicCompatibleModelClient(
            model="t", base_url="https://x.example.com/v1",
            api_key="k", temperature=0, timeout=3,
        )
        with pytest.raises(ProviderError) as exc_info:
            client.complete("p", max_new_tokens=10)

        assert exc_info.value.code == "invalid_json"

    def test_empty_text(self, monkeypatch):
        """content 数组中没有 text 类型。"""
        returns = [
            _FakeResponse(
                _body({"content": [{"type": "image", "source": {}}]}),
                content_type="application/json",
            )
        ]
        monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(returns))
        client = AnthropicCompatibleModelClient(
            model="t", base_url="https://x.example.com/v1",
            api_key="k", temperature=0, timeout=3,
        )
        with pytest.raises(ProviderError) as exc_info:
            client.complete("p", max_new_tokens=10)

        assert exc_info.value.code == "empty_response"

    def test_remote_disconnected_retry(self, monkeypatch):
        """RemoteDisconnected 可重试。"""
        disconnects = [RemoteDisconnected("boom") for _ in range(3)]
        monkeypatch.setattr(
            urllib.request, "urlopen",
            _make_fake_urlopen([], raises=disconnects),
        )
        client = AnthropicCompatibleModelClient(
            model="t", base_url="https://x.example.com/v1",
            api_key="k", temperature=0, timeout=3,
        )
        with pytest.raises(ProviderError) as exc_info:
            client.complete("p", max_new_tokens=10)

        assert exc_info.value.retryable is True
        assert exc_info.value.code == "network_error"


# ── sanitize_url ──────────────────────────────────────────────


class TestSanitizeUrl:
    def test_strips_query_and_fragment(self):
        from codecat.providers.errors import sanitize_url
        result = sanitize_url("https://api.example.com/v1/chat?key=secret#frag")
        assert "key=secret" not in result
        assert "#frag" not in result
        assert result.startswith("https://")

    def test_preserves_scheme_host_path(self):
        from codecat.providers.errors import sanitize_url
        result = sanitize_url("https://api.example.com/v1/messages")
        assert result == "https://api.example.com/v1/messages"

    def test_empty_url(self):
        from codecat.providers.errors import sanitize_url
        assert sanitize_url("") == ""
        assert sanitize_url(None) == ""


# ── usage cache details extraction ────────────────────────────


class TestUsageCacheDetails:
    def test_openai_style_cache(self):
        from codecat.providers.clients import _extract_usage_cache_details
        result = _extract_usage_cache_details({
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_tokens_details": {"cached_tokens": 80},
            }
        })
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 20
        assert result["cached_tokens"] == 80
        assert result["cache_hit"] is True

    def test_no_cache_usage(self):
        from codecat.providers.clients import _extract_usage_cache_details
        result = _extract_usage_cache_details({
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}
        })
        assert result["input_tokens"] == 10
        assert result["output_tokens"] == 2
        assert result["cached_tokens"] == 0
        assert result["cache_hit"] is False

    def test_empty_usage(self):
        from codecat.providers.clients import _extract_usage_cache_details
        result = _extract_usage_cache_details({})
        assert result["input_tokens"] is None
        assert result["cached_tokens"] == 0
