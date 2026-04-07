"""
JSON Schema → Pydantic 모델 변환 유틸리티
"""
from typing import Optional, Any

from pydantic import BaseModel, Field, create_model


def _resolve_schema_type(prop_schema: dict) -> str:
    """JSON Schema에서 실제 타입 문자열을 추출.

    anyOf/oneOf 안에 null이 아닌 타입이 있으면 해당 타입을 반환.
    """
    if "type" in prop_schema:
        return prop_schema["type"]
    for key in ("anyOf", "oneOf"):
        variants = prop_schema.get(key)
        if not variants:
            continue
        for variant in variants:
            vtype = variant.get("type")
            if vtype and vtype != "null":
                return vtype
    return "string"


def create_pydantic_model_from_schema(name: str, schema: dict) -> type[BaseModel]:
    """MCP input_schema에서 Pydantic 모델 동적 생성.

    MCP 서버 내부용 파라미터(ctx 등)는 LLM에 노출하지 않도록 필터링합니다.
    """
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    _INTERNAL_PARAMS = {"ctx"}

    fields = {}
    for prop_name, prop_schema in properties.items():
        if prop_name in _INTERNAL_PARAMS:
            continue

        prop_type = _resolve_schema_type(prop_schema)
        description = prop_schema.get("description", "")

        type_mapping = {
            "string": str, "integer": int, "number": float,
            "boolean": bool, "array": list, "object": dict,
        }
        python_type = type_mapping.get(prop_type, Any)

        if prop_name in required and prop_name not in _INTERNAL_PARAMS:
            fields[prop_name] = (python_type, Field(description=description))
        else:
            fields[prop_name] = (Optional[python_type], Field(default=None, description=description))

    if not fields:
        return create_model(f"{name}Input")
    return create_model(f"{name}Input", **fields)
