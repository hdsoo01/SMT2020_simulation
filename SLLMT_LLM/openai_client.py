"""OpenAI 어댑터 — 공식 openai SDK 의 Responses API (2026-11-10).

GPT-6 계열은 도구 호출에 Responses API 가 필요하다 (developers.openai.com function-calling 안내, 2026-11-10 확인).
agent 의 대화 기록은 Gemini 모양 그대로 두고, 요청을 보낼 때 Responses 입력 항목으로 바꾼다.
- 모델 차례: 응답 output 항목(reasoning·message·function_call)을 content["_raw"] 에 그대로 두고 다음 요청에 순서대로 다시 보낸다
  (store=False 라 서버에 대화를 남기지 않음. reasoning 항목은 암호화된 채로 돌려줌)
- 도구 결과: Gemini functionResponse(id = call_id) → function_call_output 항목
- 답이 없는 function_call 이 남으면 자리 채움 결과를 넣어 요청이 깨지지 않게 함
- 도구 정의는 strict=False (기존 도구 스키마를 그대로 씀)
- 목록에 없는 모델은 'openai:<모델 이름>' 으로 (앞의 'openai:' 는 떼고 보냄). effort 는 환경변수 SLLMT_OPENAI_EFFORT 가 있을 때만 보냄
키: 대시보드 입력 또는 환경변수 OPENAI_API_KEY. 파일·로그에 남기지 않는다.
"""
from __future__ import annotations

import json
import os

import openai

import gemini_client as gc


class OpenAIError(gc.GeminiError):
    pass


PLACEHOLDER = {"error": "이 도구 호출의 결과가 기록되지 않았다 (실행 중 중단)"}


def tools_for(tool_specs: list) -> list:
    return [{"type": "function", "name": t["name"], "description": t["description"],
             "parameters": t["parameters"], "strict": False} for t in tool_specs]


def _output(call_id: str, result) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": json.dumps(result, ensure_ascii=False)}


def to_input(contents: list) -> list:
    """Gemini 모양 대화 기록 → Responses API input 항목 목록."""
    items, pending = [], []                                        # pending: 결과를 아직 못 받은 call_id
    for c in contents:
        parts = c.get("parts") or []
        if c.get("role") == "model":
            items += [_output(i, PLACEHOLDER) for i in pending]
            if "_raw" in c:
                raw = list(c["_raw"])
            else:                                                  # 어댑터가 만들지 않은 모델 차례 (agent 가 쓴 대기 안내 글 등)
                raw = []
                for n, p in enumerate(parts):
                    if p.get("text"):
                        raw.append({"role": "assistant", "content": p["text"]})
                    elif "functionCall" in p:
                        fc = p["functionCall"]
                        raw.append({"type": "function_call", "call_id": fc.get("id") or f"call_{len(items)}_{n}",
                                    "name": fc["name"], "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False)})
            items += raw
            pending = [r["call_id"] for r in raw if r.get("type") == "function_call"]
            continue
        answered = {(p.get("functionResponse") or {}).get("id") for p in parts if "functionResponse" in p}
        items += [_output(i, PLACEHOLDER) for i in pending if i not in answered]
        pending = []
        for p in parts:
            fr = p.get("functionResponse")
            if fr:
                items.append(_output(fr.get("id"), (fr.get("response") or {}).get("result")))
            elif p.get("text"):
                items.append({"role": "user", "content": p["text"]})
    items += [_output(i, PLACEHOLDER) for i in pending]
    return items


def to_gemini(resp: dict) -> dict:
    """Responses API 응답(dict) → agent 가 읽는 Gemini 응답 모양. 원래 output 항목은 content['_raw'] 에 그대로."""
    status = resp.get("status") or ""
    if status == "failed":
        raise OpenAIError(f"응답 실패: {json.dumps(resp.get('error'), ensure_ascii=False)[:500]}")
    out = list(resp.get("output") or [])
    parts = []
    for item in out:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") == "output_text" and c.get("text"):
                    parts.append({"text": c["text"]})
                elif c.get("type") == "refusal" and c.get("refusal"):
                    parts.append({"text": f"(모델이 답을 거절함: {c['refusal']})"})
        elif item.get("type") == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            parts.append({"functionCall": {"name": item["name"], "args": args if isinstance(args, dict) else {},
                                           "id": item["call_id"]}})
    finish = "STOP" if status in ("completed", "") else \
        str((resp.get("incomplete_details") or {}).get("reason") or status).upper()
    u = resp.get("usage") or {}
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
    return {"candidates": [{"content": {"role": "model", "parts": parts, "_raw": out}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": u.get("input_tokens") or 0,
                              "candidatesTokenCount": u.get("output_tokens") or 0,
                              "cachedContentTokenCount": cached},
            "modelVersion": resp.get("model")}


class OpenAIClient:
    reports_cache = True                                           # agent 가 캐시 토큰을 따로 셈

    def __init__(self, model: str, *, key: str | None = None, timeout: float = 300.0, max_retries: int = 4,
                 base_url: str | None = None):
        """key 를 주면 그것(대시보드에서 사용자가 넣은 자기 키)을, 없으면 환경변수 OPENAI_API_KEY. 키는 메모리에만 둔다."""
        self.model = model
        self.api_model = model[len("openai:"):] if model.lower().startswith("openai:") else model
        key = (key or "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise OpenAIError("OpenAI API 키가 없다. 대시보드에 넣거나 환경변수 OPENAI_API_KEY 를 설정한다")
        kw = {"base_url": base_url} if base_url else {}
        self._client = openai.OpenAI(api_key=key, timeout=timeout, max_retries=max_retries, **kw)
        self.effort = os.environ.get("SLLMT_OPENAI_EFFORT", "").strip().lower() or None
        self.sent = 0                                              # 요청 수 (SDK 안의 재시도는 따로 세지 못함)
        self.ok = 0

    def generate(self, contents: list, *, system: str, tool_specs: list) -> dict:
        kw = dict(model=self.api_model, instructions=system, input=to_input(contents), store=False)
        if tool_specs:
            kw["tools"] = tools_for(tool_specs)
        if self.effort:
            kw["reasoning"] = {"effort": self.effort}
        self.sent += 1
        try:
            resp = self._client.responses.create(**kw)
        except openai.APIStatusError as e:
            raise OpenAIError(f"HTTP {e.status_code}: {str(e)[:800]}") from None
        except openai.APITimeoutError as e:
            raise OpenAIError(f"응답 시간 초과: {e}") from None
        except openai.APIConnectionError as e:
            raise OpenAIError(f"연결 실패: {e}") from None
        self.ok += 1
        return to_gemini(resp.to_dict(mode="json"))
