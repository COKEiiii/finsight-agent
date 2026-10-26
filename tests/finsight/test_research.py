import json

import httpx

from finsight.research import ChatAdapter, demo_content, research_candidate
from finsight.screening import screen


class FakeAdapter:
    model = "local-test"
    provider = "ollama"
    base_url = "http://local-test"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def complete(self, messages, tools, final=False):
        self.messages.append(json.loads(json.dumps(messages)))
        return {"message": next(self.responses), "input_tokens": 20, "output_tokens": 10}


def response(content):
    return {"role": "assistant", "content": json.dumps(content), "tool_calls": []}


def test_multiple_tools_paired_in_one_assistant_turn_and_cached(bundle, store):
    result = screen(bundle)
    candidate = result.candidates[0]
    calls = [{"id": key, "type": "function", "function": {"name": "read_evidence", "arguments": json.dumps({"evidence_id": key})}}
             for key in ["financials", "market"]]
    adapter = FakeAdapter([{"content": "", "tool_calls": calls}, response(demo_content(candidate).model_dump())])
    card = research_candidate(result, candidate, store, adapter)
    assert card.status == "completed" and card.tool_calls == 2 and card.llm_calls == 2
    second_turn = adapter.messages[1]
    assert [m["tool_call_id"] for m in second_turn if m["role"] == "tool"] == ["financials", "market"]
    assert next(m for m in second_turn if m["role"] == "assistant")["tool_calls"] == calls
    assert research_candidate(result, candidate, store, adapter).cache_hit
    assert len(adapter.messages) == 2


def test_unknown_evidence_and_extra_rank_field_rejected(bundle, store):
    result = screen(bundle)
    bad = demo_content(result.candidates[0]).model_dump()
    bad["reasons"][0]["evidence_ids"] = ["invented-news"]
    adapter = FakeAdapter([response(bad)] * 3)
    card = research_candidate(result, result.candidates[0], store, adapter)
    assert card.status == "failed" and card.content is None and card.llm_calls == 3
    assert result.candidates == screen(bundle).candidates


def test_tool_budget_prevents_partial_execution(bundle, store):
    result = screen(bundle)
    calls = [{"id": str(i), "type": "function", "function": {"name": "read_evidence", "arguments": '{}'}} for i in range(7)]
    adapter = FakeAdapter([{"content": "", "tool_calls": calls}] * 3)
    card = research_candidate(result, result.candidates[0], store, adapter)
    assert card.status == "failed" and card.tool_calls == 0 and card.llm_calls == 3


def test_request_failures_count_towards_budget(bundle, store):
    class Offline(FakeAdapter):
        def complete(self, *args, **kwargs):
            raise httpx.ConnectError("offline")
    result = screen(bundle)
    card = research_candidate(result, result.candidates[0], store, Offline([]))
    assert card.status == "failed" and card.llm_calls == 3


def test_local_adapter_uses_native_ollama_wire_format():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "", "tool_calls": [
            {"function": {"name": "read_evidence", "arguments": {"evidence_id": "market"}}}]},
            "prompt_eval_count": 5, "eval_count": 6})
    adapter = ChatAdapter("ollama", "local", httpx.Client(transport=httpx.MockTransport(handler)))
    value = adapter.complete([{"role": "user", "content": "hello"}], [])
    assert requests[0]["stream"] is False and requests[0]["think"] is False
    assert requests[0]["options"]["num_predict"] == 1500
    assert json.loads(value["message"]["tool_calls"][0]["function"]["arguments"]) == {"evidence_id": "market"}


def test_deepseek_preserves_tool_ids_and_disables_thinking(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": '{}'}}], "usage": {}})
    adapter = ChatAdapter("deepseek", "test", httpx.Client(transport=httpx.MockTransport(handler)))
    messages = [{"role": "tool", "tool_call_id": "call-42", "name": "read_evidence", "content": '{}'}]
    adapter.complete(messages, [], final=True)
    assert seen[0]["messages"][0]["tool_call_id"] == "call-42"
    assert seen[0]["thinking"] == {"type": "disabled"}


def test_offline_template_is_explicitly_labeled(bundle, store):
    result = screen(bundle)
    card = research_candidate(result, result.candidates[0], store, demo=True)
    assert card.status == "demo" and card.llm_calls == 0
