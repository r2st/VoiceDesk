"""The OpenRouter client and its free-model fallback chain (design doc §4.3).

Every model call the voice pipeline makes goes through here, against free-tier
models that are rate limited, frequently overloaded and loose about their own
output format. The fallback chain is the whole point of the class, so most of
these tests are about a model failing in some new way and the next one in the
chain still getting its turn.

`extract_json` gets the same treatment: free models ignore `response_format`
and wrap JSON in fences or chat, and the parser is what stands between that and
the NLU layer.
"""

from __future__ import annotations

from collections import deque

import httpx
import pytest

from app.core.errors import ExternalServiceError
from app.services.llm import (
    RETRYABLE_STATUS,
    LLMMessage,
    OpenRouterClient,
    extract_json,
    get_llm_client,
    set_llm_client,
)

MODELS = ["model-a", "model-b", "model-c"]


def completion(content: str = "Namaste!", **body) -> dict:
    """A minimal well-formed OpenRouter chat completion."""
    payload = {
        "model": "model-a",
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    payload.update(body)
    return payload


def make_client(*responses: httpx.Response | Exception) -> tuple[httpx.AsyncClient, list]:
    seen: list[httpx.Request] = []
    queued = deque(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queued.popleft() if queued else httpx.Response(200, json=completion())
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


def llm(*responses: httpx.Response | Exception) -> tuple[OpenRouterClient, list]:
    client, seen = make_client(*responses)
    return OpenRouterClient(api_key="sk-test", models=list(MODELS), client=client), seen


def models_tried(seen: list[httpx.Request]) -> list[str]:
    import json

    return [json.loads(request.content)["model"] for request in seen]


# --------------------------------------------------------------------------- #
# The fallback chain
# --------------------------------------------------------------------------- #
class TestModelFallback:
    async def test_a_working_first_model_is_the_only_one_called(self):
        client, seen = llm(httpx.Response(200, json=completion()))

        result = await client.chat([LLMMessage("user", "hello")])

        assert result.content == "Namaste!"
        assert models_tried(seen) == ["model-a"]

    @pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
    async def test_a_retryable_status_moves_to_the_next_model(self, status: int):
        client, seen = llm(
            httpx.Response(status, text="busy"), httpx.Response(200, json=completion("second"))
        )

        result = await client.chat([LLMMessage("user", "hello")])

        assert result.content == "second"
        assert models_tried(seen) == ["model-a", "model-b"]

    async def test_a_transport_error_moves_to_the_next_model(self):
        client, seen = llm(
            httpx.ConnectError("reset"), httpx.Response(200, json=completion("second"))
        )

        result = await client.chat([LLMMessage("user", "hello")])

        assert result.content == "second"
        assert models_tried(seen) == ["model-a", "model-b"]

    async def test_a_timeout_moves_to_the_next_model(self):
        client, _ = llm(httpx.ReadTimeout("slow"), httpx.Response(200, json=completion("second")))

        assert (await client.chat([LLMMessage("user", "hi")])).content == "second"

    async def test_a_rejected_request_moves_to_the_next_model(self):
        """A 400 is not retryable, but the next model may still accept it."""
        client, seen = llm(
            httpx.Response(400, text="model does not support json mode"),
            httpx.Response(200, json=completion("second")),
        )

        result = await client.chat([LLMMessage("user", "hello")])

        assert result.content == "second"
        assert models_tried(seen) == ["model-a", "model-b"]

    async def test_an_empty_completion_moves_to_the_next_model(self):
        client, _ = llm(
            httpx.Response(200, json=completion("")), httpx.Response(200, json=completion("second"))
        )

        assert (await client.chat([LLMMessage("user", "hi")])).content == "second"

    async def test_a_whitespace_only_completion_moves_to_the_next_model(self):
        client, _ = llm(
            httpx.Response(200, json=completion("   \n ")),
            httpx.Response(200, json=completion("second")),
        )

        assert (await client.chat([LLMMessage("user", "hi")])).content == "second"

    async def test_a_response_with_no_choices_moves_to_the_next_model(self):
        client, _ = llm(
            httpx.Response(200, json={"model": "model-a", "choices": []}),
            httpx.Response(200, json=completion("second")),
        )

        assert (await client.chat([LLMMessage("user", "hi")])).content == "second"

    async def test_a_non_json_body_moves_to_the_next_model(self):
        """A gateway in front of a free model answering 200 with an HTML page.

        Decoding it raises `JSONDecodeError` from inside the loop, which used to
        abandon the whole chain — so one flaky model took down the two healthy
        ones behind it, which is precisely what the chain exists to prevent.
        """
        client, seen = llm(
            httpx.Response(200, text="<html>502 Bad Gateway</html>"),
            httpx.Response(200, json=completion("second")),
        )

        result = await client.chat([LLMMessage("user", "hello")])

        assert result.content == "second"
        assert models_tried(seen) == ["model-a", "model-b"]

    async def test_a_malformed_choice_moves_to_the_next_model(self):
        """Some free models return `choices: ["text"]` rather than objects."""
        client, _ = llm(
            httpx.Response(200, json={"model": "model-a", "choices": ["just a string"]}),
            httpx.Response(200, json=completion("second")),
        )

        assert (await client.chat([LLMMessage("user", "hi")])).content == "second"

    async def test_a_non_numeric_token_count_moves_to_the_next_model(self):
        client, _ = llm(
            httpx.Response(200, json=completion(usage={"prompt_tokens": "lots"})),
            httpx.Response(200, json=completion("second")),
        )

        assert (await client.chat([LLMMessage("user", "hi")])).content == "second"

    async def test_the_chain_is_walked_in_order(self):
        client, seen = llm(
            httpx.Response(429, text="busy"),
            httpx.Response(429, text="busy"),
            httpx.Response(200, json=completion("third")),
        )

        result = await client.chat([LLMMessage("user", "hello")])

        assert result.content == "third"
        assert models_tried(seen) == MODELS

    async def test_every_model_failing_is_an_external_service_error(self):
        client, seen = llm(*[httpx.Response(429, text="busy") for _ in MODELS])

        with pytest.raises(ExternalServiceError) as exc:
            await client.chat([LLMMessage("user", "hello")])

        assert exc.value.details["models"] == MODELS
        assert "429" in exc.value.details["last_error"]
        assert len(seen) == len(MODELS)

    async def test_the_last_error_names_the_model_that_failed(self):
        client, _ = llm(*[httpx.ConnectError("reset") for _ in MODELS])

        with pytest.raises(ExternalServiceError) as exc:
            await client.chat([LLMMessage("user", "hello")])

        assert "model-c" in exc.value.details["last_error"]

    async def test_an_explicit_chain_overrides_the_configured_one(self):
        client, seen = llm(httpx.Response(200, json=completion()))

        await client.chat([LLMMessage("user", "hi")], models=["model-z"])

        assert models_tried(seen) == ["model-z"]

    async def test_no_configured_models_is_an_external_service_error(self):
        http, seen = make_client()
        client = OpenRouterClient(api_key="sk-test", models=[], client=http)

        with pytest.raises(ExternalServiceError):
            await client.chat([LLMMessage("user", "hi")])

        assert seen == []

    async def test_a_missing_api_key_is_an_external_service_error(self):
        http, seen = make_client()
        client = OpenRouterClient(api_key="", models=list(MODELS), client=http)

        with pytest.raises(ExternalServiceError) as exc:
            await client.chat([LLMMessage("user", "hi")])

        assert "OPENROUTER_API_KEY" in str(exc.value)
        assert seen == []


# --------------------------------------------------------------------------- #
# What goes on the wire
# --------------------------------------------------------------------------- #
class TestRequestShape:
    async def test_posts_to_the_chat_completions_endpoint(self):
        client, seen = llm(httpx.Response(200, json=completion()))

        await client.chat([LLMMessage("user", "hi")])

        assert seen[0].method == "POST"
        assert str(seen[0].url).endswith("/chat/completions")

    async def test_sends_the_api_key_as_a_bearer_token(self):
        client, seen = llm(httpx.Response(200, json=completion()))

        await client.chat([LLMMessage("user", "hi")])

        assert seen[0].headers["Authorization"] == "Bearer sk-test"

    async def test_sends_the_attribution_headers_openrouter_bills_against(self):
        client, seen = llm(httpx.Response(200, json=completion()))

        await client.chat([LLMMessage("user", "hi")])

        assert seen[0].headers["HTTP-Referer"]
        assert seen[0].headers["X-Title"]

    async def test_message_objects_are_serialised(self):
        import json

        client, seen = llm(httpx.Response(200, json=completion()))

        await client.chat([LLMMessage("system", "be brief"), LLMMessage("user", "hi")])

        assert json.loads(seen[0].content)["messages"] == [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ]

    async def test_plain_dicts_are_accepted_too(self):
        import json

        client, seen = llm(httpx.Response(200, json=completion()))

        await client.chat([{"role": "user", "content": "hi"}])

        assert json.loads(seen[0].content)["messages"] == [{"role": "user", "content": "hi"}]

    async def test_sampling_parameters_are_passed_through(self):
        import json

        client, seen = llm(httpx.Response(200, json=completion()))

        await client.chat([LLMMessage("user", "hi")], temperature=0.9, max_tokens=50)

        body = json.loads(seen[0].content)
        assert body["temperature"] == 0.9
        assert body["max_tokens"] == 50

    async def test_response_format_is_omitted_unless_asked_for(self):
        import json

        client, seen = llm(httpx.Response(200, json=completion()))

        await client.chat([LLMMessage("user", "hi")])

        assert "response_format" not in json.loads(seen[0].content)

    async def test_a_trailing_slash_on_the_base_url_is_normalised(self):
        http, seen = make_client(httpx.Response(200, json=completion()))
        client = OpenRouterClient(
            api_key="sk-test",
            base_url="https://openrouter.test/api/v1/",
            models=["model-a"],
            client=http,
        )

        await client.chat([LLMMessage("user", "hi")])

        assert str(seen[0].url) == "https://openrouter.test/api/v1/chat/completions"


# --------------------------------------------------------------------------- #
# Reading the reply
# --------------------------------------------------------------------------- #
class TestResponseParsing:
    async def test_reads_content_tokens_and_finish_reason(self):
        client, _ = llm(httpx.Response(200, json=completion("Theek hai")))

        result = await client.chat([LLMMessage("user", "hi")])

        assert result.content == "Theek hai"
        assert result.prompt_tokens == 11
        assert result.completion_tokens == 7
        assert result.total_tokens == 18
        assert result.finish_reason == "stop"

    async def test_content_is_stripped(self):
        client, _ = llm(httpx.Response(200, json=completion("  padded  ")))

        assert (await client.chat([LLMMessage("user", "hi")])).content == "padded"

    async def test_the_model_that_actually_answered_is_reported(self):
        """OpenRouter reroutes, so the answering model is not always the asked one."""
        client, _ = llm(httpx.Response(200, json=completion(model="model-a:free-mirror")))

        assert (await client.chat([LLMMessage("user", "hi")])).model == "model-a:free-mirror"

    async def test_missing_usage_counts_as_zero_tokens(self):
        body = completion()
        del body["usage"]
        client, _ = llm(httpx.Response(200, json=body))

        result = await client.chat([LLMMessage("user", "hi")])

        assert result.total_tokens == 0

    async def test_latency_is_recorded(self):
        client, _ = llm(httpx.Response(200, json=completion()))

        assert (await client.chat([LLMMessage("user", "hi")])).latency_ms >= 0

    async def test_the_raw_body_is_retained(self):
        body = completion()
        client, _ = llm(httpx.Response(200, json=body))

        assert (await client.chat([LLMMessage("user", "hi")])).raw == body


class TestChatJson:
    async def test_asks_for_json_mode(self):
        import json

        client, seen = llm(httpx.Response(200, json=completion('{"intent": "booking"}')))

        await client.chat_json([LLMMessage("user", "classify")])

        assert json.loads(seen[0].content)["response_format"] == {"type": "json_object"}

    async def test_returns_the_parsed_object_and_the_response(self):
        client, _ = llm(httpx.Response(200, json=completion('{"intent": "booking"}')))

        parsed, response = await client.chat_json([LLMMessage("user", "classify")])

        assert parsed == {"intent": "booking"}
        assert response.content == '{"intent": "booking"}'

    async def test_unparseable_json_is_an_empty_dict_not_an_error(self):
        """The NLU layer has defaults; a refusal must not become an exception."""
        client, _ = llm(httpx.Response(200, json=completion("I cannot help with that.")))

        parsed, _ = await client.chat_json([LLMMessage("user", "classify")])

        assert parsed == {}


# --------------------------------------------------------------------------- #
# extract_json
# --------------------------------------------------------------------------- #
class TestExtractJson:
    def test_a_bare_object(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_a_json_fenced_block(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_an_unlabelled_fenced_block(self):
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_an_object_wrapped_in_prose(self):
        assert extract_json('Sure! {"a": 1} Hope that helps.') == {"a": 1}

    def test_nested_objects_survive(self):
        assert extract_json('prose {"a": {"b": [1, 2]}} more') == {"a": {"b": [1, 2]}}

    def test_leading_and_trailing_whitespace(self):
        assert extract_json('\n\n  {"a": 1}  \n') == {"a": 1}

    def test_a_top_level_array_is_rejected(self):
        """Callers index the result by key; a list would raise for them."""
        assert extract_json("[1, 2, 3]") == {}

    def test_a_fenced_array_is_rejected(self):
        assert extract_json("```json\n[1, 2]\n```") == {}

    def test_prose_with_no_json_at_all(self):
        assert extract_json("I am sorry, I cannot do that.") == {}

    def test_an_unclosed_object(self):
        assert extract_json('{"a": 1') == {}

    def test_an_empty_string(self):
        assert extract_json("") == {}

    def test_a_bare_scalar_is_rejected(self):
        assert extract_json("42") == {}

    def test_an_empty_object(self):
        assert extract_json("{}") == {}


class TestClientRegistry:
    def test_the_client_is_a_singleton(self):
        set_llm_client(None)
        try:
            assert get_llm_client() is get_llm_client()
        finally:
            set_llm_client(None)

    def test_an_injected_client_is_returned(self):
        stub = OpenRouterClient(api_key="sk-test")
        set_llm_client(stub)
        try:
            assert get_llm_client() is stub
        finally:
            set_llm_client(None)
