"""여러 LLM 제공자 연결 (2026-11-10). 모델 이름으로 제공자를 고르고, 같은 generate 계약의 어댑터를 만든다.

계약 (docs/ARCHITECTURE.md 'Agent 확장'): client.generate(contents, system=..., tool_specs=...) → Gemini 응답 모양 dict,
client.model, client.sent. 대화 기록(contents)은 지금처럼 Gemini 모양으로 두고, 다른 제공자 어댑터가 요청을 보낼 때 바꾼다.

    gemini-*                          → gemini_client.GeminiClient (표준 라이브러리, 바뀐 것 없음)
    claude-*                          → claude_client.ClaudeClient (공식 anthropic SDK, Messages API)
    gpt-* · o1/o3/o4-* · openai:<이름> → openai_client.OpenAIClient (공식 openai SDK, Responses API)

키: 대시보드 입력(브라우저 세션 메모리) 또는 환경변수 GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY.
키는 파일·로그·화면에 남기지 않는다.
"""
from __future__ import annotations

import os

import gemini_client as gc

# 모든 제공자 오류를 기존 `except gc.GeminiError` 자리(agent·app·평가기)가 그대로 받도록 같은 클래스를 쓴다
LLMError = gc.GeminiError

PROVIDERS = {
    "gemini": {"label": "Gemini (Google)", "env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
               "key_url": "https://aistudio.google.com/apikey", "free_tier": True},
    "claude": {"label": "Claude (Anthropic)", "env": ("ANTHROPIC_API_KEY",),
               "key_url": "https://platform.claude.com", "free_tier": False},
    "openai": {"label": "OpenAI", "env": ("OPENAI_API_KEY",),
               "key_url": "https://platform.openai.com/api-keys", "free_tier": False},
}

# 유료 가격 (USD / 1M 토큰): (입력, 출력, 캐시 읽기, 캐시 쓰기). Gemini 는 gemini_client.PRICES 를 그대로 쓴다.
# Claude: 2026-11-03 Anthropic 가격표 (Haiku 5.5 는 입력 100K 토큰 이하 기준, 캐시 값은 표준 배율 0.1·1.25 로 추정).
# OpenAI: 2026-11-10 developers.openai.com/api/docs/pricing 표준(272K 이하) 가격, 캐시 쓰기는 따로 값 없음.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "gpt-6-luna": (0.10, 0.50, 0.01, 0.10),
    "gpt-6.1-terra": (1.00, 5.00, 0.05, 1.00),
    "gpt-6.1-sol": (2.00, 10.00, 0.10, 2.00),
}
# 11-10 사용자 결정: 화면·CLI 기본을 3판 평가 모델로 (gemini_client.DEFAULT_MODEL 2.5-flash-lite 는 3판 파일이라 그대로 둠)
DEFAULT_MODEL = "gemini-3.5-flash-lite"
MODELS = list(gc.PRICES) + list(PRICES)                           # 화면 목록 순서: Gemini → Claude → OpenAI
OPENAI_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-", "openai:")
# 키만 넣었을 때 처음 고를 모델 (사용자가 목록에서 바꾼다). Claude 는 Anthropic 권장 기본 모델
PROVIDER_DEFAULTS = {"gemini": DEFAULT_MODEL, "claude": "claude-opus-5-5", "openai": "gpt-6-luna"}


def provider_of_key(key: str) -> str | None:
    """키 앞부분으로 회사를 알아본다 (Gemini 'AIza…', Claude 'sk-ant-…', OpenAI 'sk-…'). 모르는 모양이면 None. 키는 남기지 않는다."""
    k = (key or "").strip()
    if k.startswith("AIza"):
        return "gemini"
    if k.startswith("sk-ant-"):
        return "claude"
    if k.startswith("sk-"):
        return "openai"
    return None


def provider_of(model: str) -> str:
    m = (model or "").strip().lower()
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith("claude"):
        return "claude"
    if m.startswith(OPENAI_PREFIXES) or m == "chat-latest":
        return "openai"
    raise LLMError(f"모델 이름으로 제공자를 알 수 없다: {model!r}. gemini-…, claude-…, gpt-… 또는 openai:<모델 이름> 으로 쓴다")


def _provider_or_gemini(model: str) -> str:
    """비용 계산용: 제공자를 모르는 이름(시험용 가짜 모델 등)은 예전처럼 Gemini 계산(가격 없으면 0)을 따른다."""
    try:
        return provider_of(model)
    except LLMError:
        return "gemini"


def env_key(provider: str) -> str:
    """그 제공자의 키 환경변수 값 (없으면 빈 문자열). 값은 돌려주기만 하고 기록하지 않는다."""
    for name in PROVIDERS[provider]["env"]:
        if os.environ.get(name, "").strip():
            return os.environ[name].strip()
    return ""


def make_client(model: str, *, key: str | None = None):
    """모델 이름에 맞는 어댑터. key 가 없으면 그 제공자의 환경변수를 쓴다. SDK 는 그 제공자를 쓸 때만 읽는다."""
    provider = provider_of(model)
    if provider == "gemini":
        return gc.GeminiClient(model, key=key)
    try:
        if provider == "claude":
            import claude_client as mod
            return mod.ClaudeClient(model, key=key)
        import openai_client as mod
        return mod.OpenAIClient(model, key=key)
    except ImportError as e:
        sdk = "anthropic" if provider == "claude" else "openai"
        raise LLMError(f"{PROVIDERS[provider]['label']} 를 쓰려면 공식 SDK 가 필요하다 (pip install {sdk}): {e}") from None


def cost(model: str, usage: dict) -> float:
    """유료 기준 추정 비용. Gemini 는 예전 계산 그대로(캐시 무시), 다른 제공자는 캐시 읽기·쓰기 가격을 따로 센다."""
    if _provider_or_gemini(model) == "gemini":
        return gc.cost(model, usage)
    p = PRICES.get(model)
    if p is None:
        return 0.0
    pin, pout, pread, pwrite = p
    read, write = usage.get("cache_read", 0), usage.get("cache_write", 0)
    plain = max(0, usage.get("in", 0) - read - write)
    return (plain * pin + read * pread + write * pwrite + usage.get("out", 0) * pout) / 1e6


def cost_text(model: str, usage: dict) -> str:
    """비용 줄의 뒷부분. Gemini 는 예전 글 그대로 ('유료 기준 약 $… (무료 등급이면 0원)'), 밤샘 실행기는 '유료 기준 약 $' 를 읽는다."""
    c = cost(model, usage)
    if _provider_or_gemini(model) == "gemini":
        return f"유료 기준 약 ${c:.4f} (무료 등급이면 0원)"
    if model not in PRICES:
        return f"유료 기준 약 ${c:.4f} (가격 미등록 모델이라 0으로 둠)"
    return f"유료 기준 약 ${c:.4f} (캐시 반영 추정)"
