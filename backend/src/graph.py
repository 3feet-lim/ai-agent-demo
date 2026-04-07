"""
호환성 레이어 — nodes 패키지로 이전됨

기존 graph.py의 모든 기능은 nodes/ 패키지로 분리되었습니다.
이 파일은 하위 호환성을 위해 re-export만 수행합니다.
"""
from .nodes.state import read_state_meta, read_state_meta_json, extract_text_from_content
from .nodes.pipeline import build_main_graph

# 하위 호환: 기존 이름 유지
_extract_text_from_content = extract_text_from_content

__all__ = [
    "read_state_meta",
    "read_state_meta_json",
    "build_main_graph",
]
