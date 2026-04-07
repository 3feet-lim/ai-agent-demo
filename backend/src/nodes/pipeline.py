"""
파이프라인 조립 — 노드 인스턴스를 받아서 LangGraph 워크플로우를 구성

analyze → route
  ├─ general → direct_answer → END
  ├─ requires_validation → resolve → route
  │   ├─ validation_fail → END
  │   └─ plan → execute_steps → evaluate → report_setup → report → END
  └─ data_collection_only → plan → execute_steps → evaluate → report_setup → report → END
                                                      ↕
                                              execute_additional (재계획 루프)
"""
from typing import Any

from langchain_core.tools import BaseTool
from langgraph.graph import StateGraph, MessagesState, START, END

from .analyze import AnalyzeNode, route_after_analyze
from .resolve import ResolveNode, route_after_resolve
from .plan import PlanNode
from .execute import ExecuteStepsNode, ExecuteAdditionalNode
from .evaluate import EvaluateNode, route_after_evaluate
from .report import ReportSetupNode, ReportLLMNode
from .direct_answer import DirectAnswerNode, DirectAnswerValidationFailNode


def build_main_graph(
    main_llm,
    main_llm_with_tools,
    main_tools: list[BaseTool],
    profile_resolver,
    default_region: str,
    all_tools: list[BaseTool],
) -> Any:
    """Main Agent용 LangGraph 워크플로우 구성.

    각 노드를 인스턴스화하고 그래프 엣지를 연결합니다.

    Args:
        main_llm: 도구 없는 Main LLM (리포트/분류용)
        main_llm_with_tools: sub-agent 도구가 바인딩된 Main LLM
        main_tools: SubAgentTool 리스트
        profile_resolver: AccountProfileResolver 인스턴스
        default_region: 기본 AWS 리전
        all_tools: 모든 MCP 도구 리스트 (가드레일 설정용)
    """
    # 노드 인스턴스 생성
    analyze = AnalyzeNode(llm=main_llm, profile_resolver=profile_resolver)
    resolve = ResolveNode(
        llm_with_tools=main_llm_with_tools, main_tools=main_tools,
        profile_resolver=profile_resolver, default_region=default_region,
    )
    plan = PlanNode()
    execute_steps = ExecuteStepsNode(
        main_tools=main_tools, all_tools=all_tools,
        profile_resolver=profile_resolver,
    )
    evaluate = EvaluateNode(llm=main_llm)
    execute_additional = ExecuteAdditionalNode(
        main_tools=main_tools, all_tools=all_tools,
        profile_resolver=profile_resolver,
    )
    report_setup = ReportSetupNode(profile_resolver=profile_resolver)
    report = ReportLLMNode(llm=main_llm)
    direct_answer = DirectAnswerNode(llm=main_llm)
    validation_fail = DirectAnswerValidationFailNode()

    # 그래프 조립
    graph = StateGraph(MessagesState)

    graph.add_node("analyze", analyze)
    graph.add_node("resolve", resolve)
    graph.add_node("plan", plan)
    graph.add_node("execute_steps", execute_steps)
    graph.add_node("evaluate", evaluate)
    graph.add_node("execute_additional", execute_additional)
    graph.add_node("report_setup", report_setup)
    graph.add_node("report", report)
    graph.add_node("direct_answer", direct_answer)
    graph.add_node("direct_answer_validation_fail", validation_fail)

    # 엣지 연결
    graph.add_edge(START, "analyze")

    graph.add_conditional_edges("analyze", route_after_analyze, {
        "resolve": "resolve",
        "plan": "plan",
        "direct_answer": "direct_answer",
    })

    graph.add_conditional_edges("resolve", route_after_resolve, {
        "plan": "plan",
        "direct_answer_validation_fail": "direct_answer_validation_fail",
    })

    graph.add_edge("plan", "execute_steps")
    graph.add_edge("execute_steps", "evaluate")

    graph.add_conditional_edges("evaluate", route_after_evaluate, {
        "report_setup": "report_setup",
        "execute_additional": "execute_additional",
    })

    graph.add_edge("execute_additional", "evaluate")

    graph.add_edge("report_setup", "report")
    graph.add_edge("report", END)
    graph.add_edge("direct_answer", END)
    graph.add_edge("direct_answer_validation_fail", END)

    return graph.compile()
