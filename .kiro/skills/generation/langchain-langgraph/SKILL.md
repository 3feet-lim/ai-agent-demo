---
name: langchain-langgraph-generation
description: LangChain/LangGraph 기반 AI 에이전트 코드 생성 패턴. 체인 구성, 그래프 정의, 노드/엣지 설계, AWS Bedrock 연동, 도구(tool) 바인딩을 다룹니다.
---

# LangChain / LangGraph 코드 생성 가이드

## LangChain 패턴

### ChatBedrock 초기화
```python
from langchain_aws import ChatBedrock

llm = ChatBedrock(
    model_id="anthropic.claude-sonnet-4-5-v2:0",
    region_name="us-east-1",
)
```

### 도구(Tool) 정의
```python
from langchain_core.tools import tool

@tool
def search_documents(query: str) -> str:
    """문서를 검색합니다.

    Args:
        query: 검색 쿼리
    """
    # 구현
    return result
```

### 도구 바인딩
```python
llm_with_tools = llm.bind_tools([search_documents, another_tool])
```

## LangGraph 패턴

### 기본 그래프 구조
```python
from langgraph.graph import StateGraph, START, END
from typing import TypedDict, Annotated

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]

graph = StateGraph(AgentState)
graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", should_continue)
graph.add_edge("tools", "agent")
app = graph.compile()
```

### 노드 함수
```python
async def agent_node(state: AgentState) -> dict:
    """에이전트 노드 - LLM 호출"""
    response = await llm_with_tools.ainvoke(state["messages"])
    return {"messages": [response]}
```

### 조건부 엣지
```python
def should_continue(state: AgentState) -> str:
    """도구 호출이 필요한지 판단"""
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "tools"
    return END
```

## 규칙
- State는 TypedDict로 정의
- 노드 함수는 async def 사용
- 그래프는 compile() 후 사용
- 에러 처리는 노드 내부에서 수행
