"""
State 메타데이터 헬퍼

LangGraph MessagesState에서 __KEY__:value 형태의 메타데이터를 읽고 쓰는 유틸리티.
"""
import json
from typing import Optional

from langchain_core.messages import SystemMessage
from langgraph.graph import MessagesState


def read_state_meta(state: MessagesState, key: str) -> Optional[str]:
    """state에서 __{KEY}__:value 형태의 SystemMessage 메타데이터를 읽는 유틸.

    가장 마지막에 저장된 값을 반환합니다.
    찾지 못하면 None을 반환합니다.
    """
    prefix = f"__{key}__:"
    for m in reversed(state["messages"]):
        if isinstance(m, SystemMessage) and isinstance(m.content, str):
            if m.content.startswith(prefix):
                return m.content[len(prefix):]
    return None


def read_state_meta_json(state: MessagesState, key: str) -> Optional[dict]:
    """state에서 __{KEY}__:JSON 형태의 메타데이터를 파싱하여 dict로 반환.

    파싱 실패 시 None을 반환합니다.
    """
    raw = read_state_meta(state, key)
    if raw is None:
        return None
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        return None


def extract_text_from_content(content) -> str:
    """LLM 응답의 content에서 텍스트를 추출하는 유틸.

    content가 str이면 그대로 반환.
    content가 list (content blocks 형태)이면 text 블록들을 결합하여 반환.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)
