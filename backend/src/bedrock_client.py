"""
LangChain + LangGraph 기반 Multi-Agent Bedrock 클라이언트

Main Agent (라우팅/종합/리포트) → Sub-Agents (데이터 수집)
- Metric Agent: Grafana MCP, CloudWatch 메트릭
- Log Agent: CloudWatch Logs MCP
- Resource Agent: AWS API MCP (리소스 상태)
- Network Agent: AWS API MCP (VPC, TGW, SG, NACL)
"""
import asyncio
import time
from typing import Optional, Any

from langchain_aws import ChatBedrock
from langchain_core.messages import HumanMessage, AIMessage, AIMessageChunk, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError
from loguru import logger

from .config import get_settings
from .mcp_manager import get_mcp_manager
from .conversation_store import get_conversation_store
from .account_profile_resolver import AccountProfileResolver
from .time_utils import parse_alarm_time_window, ALARM_TIME_PATTERN
from .tools import (
    create_mcp_tool,
    classify_tool,
    build_sub_agent_graph,
    SubAgentTool,
    MCPToolWrapper,
)
from .graph import build_main_graph
from .prompts import (
    build_metric_agent_prompt,
    build_log_agent_prompt,
    build_resource_agent_prompt,
    build_network_agent_prompt,
)


class BedrockAgent:
    """
    Multi-Agent Bedrock 에이전트

    Main Agent가 Sub-Agent를 도구로 호출하는 구조.
    Main: 라우팅, 종합 분석, 리포트 작성
    Sub: 메트릭/로그/리소스/네트워크 데이터 수집
    """

    def __init__(self):
        settings = get_settings()
        self.main_model_id = settings.bedrock_model_id
        self.sub_model_id = settings.bedrock_sub_agent_model_id or settings.bedrock_model_id
        self.region = settings.aws_region
        self._mcp_manager = None
        self._all_tools: list[BaseTool] = []  # 모든 MCP 도구
        self._sub_agent_tools: dict[str, list[BaseTool]] = {}  # role → 도구 리스트
        self._sub_agent_graphs: dict[str, Any] = {}  # role → 컴파일된 그래프
        self._main_tools: list[BaseTool] = []  # Main Agent용 도구 (SubAgentTool)
        self._main_llm = None
        self._main_graph = None
        self._profile_resolver = AccountProfileResolver()
        self._initialized = False

    async def _ensure_initialized(self):
        """비동기 초기화"""
        if self._initialized:
            return

        settings = get_settings()
        self._mcp_manager = await get_mcp_manager()
        context = await self._mcp_manager.get_context()

        # MCP 도구를 LangChain 도구로 변환
        self._all_tools = []
        for mcp_tool in context.tools:
            try:
                lc_tool = create_mcp_tool(mcp_tool, self._mcp_manager)
                self._all_tools.append(lc_tool)
            except Exception as e:
                logger.warning(f"Failed to create tool {mcp_tool.name}: {e}")

        # 도구를 sub-agent별로 분류
        self._sub_agent_tools = {"metric": [], "log": [], "resource": [], "network": []}
        for lc_tool in self._all_tools:
            mcp_tool = lc_tool.mcp_tool if hasattr(lc_tool, 'mcp_tool') else None
            if mcp_tool:
                roles = classify_tool(mcp_tool)
                for role in roles:
                    if role in self._sub_agent_tools:
                        self._sub_agent_tools[role].append(lc_tool)

        # network agent는 resource agent와 같은 도구 공유
        if not self._sub_agent_tools["network"]:
            self._sub_agent_tools["network"] = list(self._sub_agent_tools["resource"])

        # Sub-agent LLM 초기화
        sub_llm = ChatBedrock(
            model_id=self.sub_model_id,
            region_name=self.region,
            model_kwargs={"max_tokens": 4096, "temperature": 0.3},
        )

        logger.info(f"[Multi-Agent] Main model: {self.main_model_id}")
        logger.info(f"[Multi-Agent] Sub model: {self.sub_model_id}")

        # Sub-agent 그래프 생성
        sub_configs = {
            "metric": (build_metric_agent_prompt, 40),
            "log": (build_log_agent_prompt, 40),
            "resource": (build_resource_agent_prompt, 30),
            "network": (build_network_agent_prompt, 40),
        }

        self._main_tools = []
        for role, (prompt_fn, rec_limit) in sub_configs.items():
            tools = self._sub_agent_tools.get(role, [])
            if tools:
                sub_llm_with_tools = sub_llm.bind_tools(tools)
                graph = build_sub_agent_graph(sub_llm_with_tools, tools)
                self._sub_agent_graphs[role] = graph
                tool_names = [t.name for t in tools]
                logger.info(f"[Multi-Agent] {role} agent: {len(tools)} tools ({', '.join(tool_names[:5])}...)")
            else:
                graph = None
                logger.warning(f"[Multi-Agent] {role} agent: no tools available")
                continue

            # Sub-agent를 Main Agent의 도구로 래핑
            sub_tool = SubAgentTool(
                name=f"collect_{role}s" if role == "metric" else
                     f"collect_{role}s" if role == "log" else
                     f"check_{role}s" if role == "resource" else
                     f"investigate_{role}",
                description=self._get_sub_agent_description(role),
                graph=graph,
                system_prompt=prompt_fn(),
                recursion_limit=rec_limit,
            )
            self._main_tools.append(sub_tool)

        # Main Agent LLM 초기화
        self._main_llm = ChatBedrock(
            model_id=self.main_model_id,
            region_name=self.region,
            model_kwargs={"max_tokens": 4096, "temperature": 0.7},
        )

        if self._main_tools:
            self._main_llm_with_tools = self._main_llm.bind_tools(self._main_tools)
            logger.info(f"[Multi-Agent] Main agent bound with {len(self._main_tools)} sub-agent tools")
        else:
            self._main_llm_with_tools = self._main_llm
            logger.warning("[Multi-Agent] No sub-agent tools available")

        # Main Agent 그래프 생성
        self._main_graph = self._build_main_graph()
        self._initialized = True
        logger.info("[Multi-Agent] 초기화 완료")

    @staticmethod
    def _get_sub_agent_description(role: str) -> str:
        """Sub-agent 도구 설명"""
        descs = {
            "metric": (
                "메트릭 수집 에이전트. Grafana(PromQL)와 CloudWatch에서 메트릭을 조회합니다. "
                "task에 조회할 지표, 리소스, 시간 범위, 리전 등을 구체적으로 명시하세요."
            ),
            "log": (
                "로그 수집 에이전트. CloudWatch Logs에서 로그를 조회합니다. "
                "task에 서비스명, 로그 그룹 힌트, 검색 키워드, 시간 범위 등을 명시하세요."
            ),
            "resource": (
                "AWS 리소스 상태 확인 에이전트. EC2, EKS, RDS, ALB 등의 상태를 조회합니다. "
                "task에 리소스 ID, 서비스 유형, 리전, 확인할 항목을 명시하세요. "
                "리스트/목록 요청 시 task에 '각 리소스의 상세 정보(이름, ID, 타입, 상태, IP, AZ 등)를 개별적으로 모두 나열하라'고 명시하세요."
            ),
            "network": (
                "네트워크 문제 조사 에이전트. VPC, TGW, SG, NACL, 라우팅 등을 조사합니다. "
                "task에 소스/대상, VPC ID, 서브넷, 연결 방식 등을 명시하세요."
            ),
        }
        return descs.get(role, "Sub-agent")

    def _build_main_graph(self) -> Any:
        """Main Agent용 LangGraph 워크플로우 생성 (graph 모듈에 위임)"""
        return build_main_graph(
            main_llm=self._main_llm,
            main_llm_with_tools=self._main_llm_with_tools,
            main_tools=self._main_tools,
            profile_resolver=self._profile_resolver,
            default_region=self.region,
            all_tools=self._all_tools,
        )

    # ── 히스토리 / 토큰 관리 ──────────────────────────────────

    WINDOW_SIZE = 20
    MAX_CONTEXT_TOKENS = 150000
    CHARS_PER_TOKEN = 3

    def _estimate_tokens(self, text: str) -> int:
        return len(text) // self.CHARS_PER_TOKEN

    def _trim_messages_by_tokens(self, messages: list, max_tokens: int | None = None) -> list:
        """LangChain 메시지 리스트를 토큰 한도 내로 트리밍"""
        if not messages:
            return messages
        max_tokens = max_tokens or self.MAX_CONTEXT_TOKENS
        used = 0
        system_msgs = []
        other_msgs = []
        for msg in messages:
            content = msg.content if hasattr(msg, "content") else str(msg)
            if isinstance(content, list):
                content = " ".join(
                    str(b.get("text", "")) if isinstance(b, dict) else str(b)
                    for b in content
                )
            if isinstance(msg, SystemMessage):
                system_msgs.append(msg)
                used += self._estimate_tokens(str(content))
            else:
                other_msgs.append((msg, self._estimate_tokens(str(content))))
        if used >= max_tokens:
            logger.warning(f"[토큰 관리] 시스템 프롬프트만으로 {used:,} 토큰")
            return messages
        remaining = max_tokens - used
        kept = []
        for msg, tokens in reversed(other_msgs):
            if remaining - tokens < 0:
                break
            kept.insert(0, msg)
            remaining -= tokens
        trimmed_count = len(other_msgs) - len(kept)
        if trimmed_count > 0:
            logger.info(
                f"[토큰 관리] 히스토리 트리밍: {len(other_msgs)}개 → {len(kept)}개 "
                f"(제거 {trimmed_count}개)"
            )
        return system_msgs + kept

    async def _summarize_messages(self, messages: list[dict]) -> str:
        """오래된 메시지들을 LLM으로 요약"""
        if not messages:
            return ""
        conversation_text = "\n".join(
            [f"{m['role']}: {m['content']}" for m in messages]
        )
        summary_prompt = [
            SystemMessage(content=(
                "You are a conversation summarizer. "
                "Summarize the following conversation concisely in Korean. "
                "Preserve key facts, decisions, tool results, and important context. "
                "Keep the summary under 500 words."
            )),
            HumanMessage(content=conversation_text),
        ]
        response = await self._main_llm.ainvoke(summary_prompt)
        if isinstance(response.content, str):
            return response.content
        return str(response.content)

    async def _build_hybrid_history(
        self, conversation_id: str, all_messages: list[dict],
    ) -> list[dict]:
        """하이브리드 메모리: 요약 + 슬라이딩 윈도우"""
        total = len(all_messages)
        if total <= self.WINDOW_SIZE:
            return all_messages
        old_messages = all_messages[:-self.WINDOW_SIZE]
        recent_messages = all_messages[-self.WINDOW_SIZE:]
        store = await get_conversation_store()
        existing = await store.get_summary(conversation_id)
        old_count = len(old_messages)
        if not existing or existing["summarized_until"] < old_count:
            if existing and existing["summarized_until"] > 0:
                new_portion = old_messages[existing["summarized_until"]:]
                combined_text = (
                    f"기존 요약:\n{existing['summary']}\n\n추가 대화:\n"
                    + "\n".join([f"{m['role']}: {m['content']}" for m in new_portion])
                )
                summary_messages = [
                    SystemMessage(content=(
                        "You are a conversation summarizer. "
                        "Merge the existing summary with the new conversation. "
                        "Respond in Korean. Keep under 500 words."
                    )),
                    HumanMessage(content=combined_text),
                ]
                response = await self._main_llm.ainvoke(summary_messages)
                summary = response.content if isinstance(response.content, str) else str(response.content)
            else:
                summary = await self._summarize_messages(old_messages)
            await store.save_summary(conversation_id, summary, old_count)
            logger.info(f"대화 요약 갱신: conversation_id={conversation_id}")
        else:
            summary = existing["summary"]
        return [
            {"role": "system", "content": f"[이전 대화 요약]\n{summary}"},
        ] + recent_messages

    def _convert_to_langchain_messages(
        self, history: list[dict], system_prompt: str,
        images: Optional[list[str]] = None,
    ) -> list:
        """대화 히스토리를 LangChain 메시지 형식으로 변환"""
        messages = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        for i, msg in enumerate(history):
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                is_last_user = (i == len(history) - 1) and images
                if is_last_user:
                    content_blocks = []
                    for img_data in images:
                        if img_data.startswith("data:"):
                            header, b64 = img_data.split(",", 1)
                            media_type = header.split(":")[1].split(";")[0]
                        else:
                            b64 = img_data
                            media_type = "image/png"
                        content_blocks.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{b64}"},
                        })
                    content_blocks.append({"type": "text", "text": content})
                    messages.append(HumanMessage(content=content_blocks))
                else:
                    messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
            elif role == "system":
                messages.append(SystemMessage(content=content))
        return messages

    def _prepare_tools_for_request(
        self, message: str, time_window: Optional[tuple[str, str]],
        event_queue=None,
    ):
        """요청별 도구 설정 (시간 강제, 프로필 주입, 이벤트 큐)"""
        resolved_profile = self._profile_resolver.resolve(message)
        for tool in self._all_tools:
            if isinstance(tool, MCPToolWrapper):
                tool.enforced_time_window = time_window
                tool.resolved_profile = resolved_profile
                tool.event_queue = event_queue

    def _build_full_system_prompt(
        self, enforced_time_window: tuple[str, str] | None = None,
    ) -> str:
        """초기 시스템 프롬프트 (최소 — 분류 전 단계)"""
        parts = []
        if enforced_time_window:
            s_utc, e_utc = enforced_time_window
            parts.append(f"__TIME_WINDOW__:{s_utc},{e_utc}")
        return "\n".join(parts) if parts else ""

    async def chat_stream(
        self, message: str, history: Optional[list[dict]] = None,
        conversation_id: Optional[str] = None,
        images: Optional[list[str]] = None,
    ):
        """스트리밍 대화 처리"""
        await self._ensure_initialized()
        history = history or []
        # 로그 prefix (conversation_id 앞 8자)
        cid = (conversation_id or "no-conv")[:8]

        time_window = parse_alarm_time_window(message)
        if time_window:
            tz_match = ALARM_TIME_PATTERN.search(message)
            original_tz = tz_match.group(3) if tz_match and tz_match.group(3) else "UTC"
            original_time = f"{tz_match.group(1)} {tz_match.group(2)}" if tz_match else "?"
            logger.info(f"[{cid}] [시간 강제] 알람 시각: {original_time} {original_tz} → {time_window}")

        # MCP 개별 도구 이벤트 전파용 큐
        mcp_event_queue: asyncio.Queue = asyncio.Queue()

        self._prepare_tools_for_request(message, time_window, event_queue=mcp_event_queue)

        system_prompt = self._build_full_system_prompt(enforced_time_window=time_window)
        current_history = history + [{"role": "user", "content": message}]

        if conversation_id and len(current_history) > self.WINDOW_SIZE:
            current_history = await self._build_hybrid_history(
                conversation_id, current_history
            )

        messages = self._convert_to_langchain_messages(
            current_history, system_prompt, images=images,
        )
        messages = self._trim_messages_by_tokens(messages)

        # 노드별 사용자 표시 메시지 (비전문가도 이해할 수 있도록)
        _NODE_PHASE_LABELS = {
            "analyze": "🔍 질문을 분석하여 어떤 정보가 필요한지 파악하고 있습니다...",
            "resolve": "🔎 요청하신 서버/서비스가 실제로 존재하는지 확인하고 있습니다...",
            "plan": "📋 어떤 순서로 정보를 수집할지 계획을 세우고 있습니다...",
            "execute_steps": "⚙️ 계획에 따라 모니터링 데이터를 수집하고 있습니다...",
            "report_setup": "📊 수집된 데이터를 분석 리포트로 정리하고 있습니다...",
            "report": "✍️ 최종 분석 리포트를 작성하고 있습니다...",
            "direct_answer": "💬 답변을 작성하고 있습니다...",
            "direct_answer_validation_fail": "⚠️ 요청하신 리소스를 찾을 수 없어 안내를 준비하고 있습니다...",
        }

        # Sub-agent 도구명 → 비전문가용 한국어 설명
        _TOOL_DISPLAY_NAMES = {
            # Main → Sub-agent 호출
            "collect_metrics": "📈 성능 지표(CPU, 메모리 등) 수집 에이전트",
            "collect_logs": "📋 로그 수집 에이전트",
            "check_resources": "🖥️ 서버/서비스 상태 확인 에이전트",
            "investigate_network": "🌐 네트워크 연결 상태 조사 에이전트",
        }

        stream_start = time.monotonic()
        first_token_time = None
        tool_call_count = 0
        token_count = 0
        active_tools: set[str] = set()  # 현재 실행 중인 sub-agent 추적
        current_phase: str = ""  # 현재 노드 추적 (중복 방지)

        try:
            # astream 이벤트와 MCP 큐 이벤트를 병합하기 위해
            # astream을 별도 태스크로 실행하고, 통합 큐로 합침
            merged_queue: asyncio.Queue = asyncio.Queue()
            _STREAM_END = object()

            async def _pump_stream():
                """astream 이벤트를 merged_queue로 펌핑"""
                try:
                    async for msg, metadata in self._main_graph.astream(
                        {"messages": messages},
                        config={"recursion_limit": 15},
                        stream_mode="messages",
                    ):
                        await merged_queue.put(("stream", msg, metadata))
                except Exception as e:
                    await merged_queue.put(("error", e, None))
                finally:
                    await merged_queue.put(("end", _STREAM_END, None))

            async def _pump_mcp():
                """MCP 이벤트 큐를 merged_queue로 펌핑"""
                while True:
                    evt = await mcp_event_queue.get()
                    if evt is _STREAM_END:
                        break
                    await merged_queue.put(("mcp", evt, None))

            stream_task = asyncio.create_task(_pump_stream())
            mcp_task = asyncio.create_task(_pump_mcp())

            while True:
                kind, payload, metadata = await merged_queue.get()

                if kind == "mcp":
                    yield payload
                    continue
                if kind == "error":
                    raise payload
                if kind == "end":
                    # 스트림 종료 → MCP 펌프도 종료 신호
                    await mcp_event_queue.put(_STREAM_END)
                    break

                # kind == "stream" — 기존 astream 메시지 처리
                msg = payload
                node = metadata.get("langgraph_node", "")

                # 노드 전환 시 phase 이벤트 발행
                if node and node != current_phase and node in _NODE_PHASE_LABELS:
                    current_phase = node
                    yield {"type": "phase", "name": node,
                           "message": _NODE_PHASE_LABELS[node]}

                # tool_calls가 있는 AIMessage/AIMessageChunk → sub-agent 호출 시작
                if isinstance(msg, (AIMessage, AIMessageChunk)):
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        for tc in msg.tool_calls:
                            tool_name = tc.get("name", "")
                            if tool_name and tool_name not in active_tools:
                                active_tools.add(tool_name)
                                tool_call_count += 1
                                display = _TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                                logger.info(
                                    f"[{cid}] Sub-agent 호출 #{tool_call_count}: {tool_name} "
                                    f"(경과: {time.monotonic() - stream_start:.1f}s)"
                                )
                                yield {"type": "tool_start", "name": tool_name,
                                       "display": display,
                                       "args": tc.get("args", {})}

                    # report 또는 direct_answer 노드에서 나온 텍스트 토큰만 전달
                    if node in ("report", "direct_answer", "direct_answer_validation_fail") and isinstance(msg, AIMessageChunk):
                        content = msg.content
                        if isinstance(content, str) and content:
                            if first_token_time is None:
                                first_token_time = time.monotonic()
                                logger.info(f"[{cid}] 첫 토큰: "
                                            f"{first_token_time - stream_start:.1f}s")
                            token_count += 1
                            yield {"type": "token", "content": content}
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block.get("text", "")
                                    if text:
                                        if first_token_time is None:
                                            first_token_time = time.monotonic()
                                        token_count += 1
                                        yield {"type": "token", "content": text}

                    # validate_fail 등 비스트리밍 노드의 완성된 AIMessage 처리
                    if node in ("direct_answer_validation_fail",) and isinstance(msg, AIMessage) and not isinstance(msg, AIMessageChunk):
                        content = msg.content
                        if isinstance(content, str) and content:
                            if first_token_time is None:
                                first_token_time = time.monotonic()
                            token_count += 1
                            yield {"type": "token", "content": content}

                # ToolMessage → sub-agent 완료
                elif isinstance(msg, ToolMessage):
                    tool_name = msg.name or "unknown"
                    result_str = str(msg.content) if msg.content else ""
                    is_error = (
                        not result_str or len(result_str.strip()) < 5
                        or result_str.startswith("Sub-agent error:")
                    )

                    # execute_steps 노드에서 나온 ToolMessage는 tool_start가 선행되지 않으므로
                    # tool_start → tool_end를 연속 발행
                    if tool_name not in active_tools:
                        active_tools.add(tool_name)
                        tool_call_count += 1
                        display = _TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                        logger.info(
                            f"[{cid}] Sub-agent 실행 #{tool_call_count}: {tool_name} "
                            f"(경과: {time.monotonic() - stream_start:.1f}s)"
                        )
                        yield {"type": "tool_start", "name": tool_name,
                               "display": display, "args": {}}

                    logger.info(
                        f"[{cid}] tool_end: {tool_name}, 결과 길이={len(result_str)}, "
                        f"에러={is_error} (경과: {time.monotonic() - stream_start:.1f}s)"
                    )
                    display = _TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                    active_tools.discard(tool_name)
                    yield {"type": "tool_end", "name": tool_name,
                           "display": display, "success": not is_error}

                # SystemMessage → 실행 계획 등 메타데이터 감지
                elif isinstance(msg, SystemMessage):
                    content = msg.content if isinstance(msg.content, str) else ""
                    if content.startswith("__EXECUTION_PLAN__:"):
                        try:
                            plan = json.loads(content[len("__EXECUTION_PLAN__:"):])
                            yield {"type": "execution_plan", "plan": plan}
                        except json.JSONDecodeError:
                            pass

            # 태스크 정리
            await asyncio.gather(stream_task, mcp_task, return_exceptions=True)

            total_time = time.monotonic() - stream_start
            logger.info(f"[{cid}] 완료: {total_time:.1f}s, sub-agent 호출 {tool_call_count}회, "
                        f"토큰 {token_count}개")

            # sub-agent는 호출됐는데 토큰이 하나도 없으면 → 최종 응답 생성 실패
            if tool_call_count > 0 and token_count == 0:
                logger.error(f"[{cid}] Main Agent가 최종 응답을 생성하지 못함! "
                             f"sub-agent {tool_call_count}회 호출 후 토큰 0개")
                yield {
                    "type": "token",
                    "content": (
                        "\n\n---\n"
                        "⚠️ **분석 데이터는 수집되었으나 최종 리포트 생성에 실패했습니다.**\n\n"
                        "다시 시도해 주세요. 질문 범위를 좁히면 성공률이 높아집니다."
                    ),
                }

        except GraphRecursionError:
            logger.warning(f"[{cid}] Main agent 도구 호출 제한 도달")
            yield {
                "type": "token",
                "content": (
                    "\n\n---\n"
                    "⚠️ **Sub-agent 호출 횟수 제한에 도달하여 분석을 중단합니다.**\n\n"
                    "위 내용은 제한 도달 전까지 수집된 정보 기반입니다. "
                    "범위를 좁혀서 다시 질문해 주세요."
                ),
            }
        except Exception as e:
            logger.error(f"[{cid}] Error during chat_stream: {e}", exc_info=True)
            yield {
                "type": "token",
                "content": (
                    "\n\n---\n"
                    f"⚠️ **분석 중 오류가 발생했습니다.**\n\n"
                    f"오류: {type(e).__name__}: {str(e)[:200]}\n\n"
                    "다시 시도해 주세요."
                ),
            }

    async def chat(
        self, message: str, history: Optional[list[dict]] = None,
        conversation_id: Optional[str] = None,
    ) -> str:
        """비스트리밍 대화 처리 (webhook용)"""
        await self._ensure_initialized()
        history = history or []

        time_window = parse_alarm_time_window(message)
        self._prepare_tools_for_request(message, time_window)

        system_prompt = self._build_full_system_prompt(enforced_time_window=time_window)
        current_history = history + [{"role": "user", "content": message}]

        if conversation_id and len(current_history) > self.WINDOW_SIZE:
            current_history = await self._build_hybrid_history(
                conversation_id, current_history
            )

        messages = self._convert_to_langchain_messages(current_history, system_prompt)
        messages = self._trim_messages_by_tokens(messages)

        try:
            result = await self._main_graph.ainvoke(
                {"messages": messages}, {"recursion_limit": 15},
            )
            ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
            if ai_messages:
                last = ai_messages[-1]
                if isinstance(last.content, str):
                    return last.content
                elif isinstance(last.content, list):
                    texts = [c.get("text", "") for c in last.content
                             if isinstance(c, dict) and "text" in c]
                    return "\n".join(texts) if texts else str(last.content)
                return str(last.content)
            return "응답을 생성할 수 없습니다."

        except GraphRecursionError:
            logger.warning("Main agent 도구 호출 제한 도달 (chat)")
            try:
                ai_msgs = [m for m in result["messages"] if isinstance(m, AIMessage)]
                if ai_msgs and isinstance(ai_msgs[-1].content, str):
                    return ai_msgs[-1].content + "\n\n⚠️ Sub-agent 호출 제한 도달."
            except Exception:
                pass
            return "⚠️ Sub-agent 호출 제한에 도달하여 분석을 완료하지 못했습니다."

        except Exception as e:
            logger.error(f"Error during chat: {e}")
            raise


# ── 싱글톤 ──────────────────────────────────────────────────

_agent: Optional[BedrockAgent] = None


async def get_bedrock_agent() -> BedrockAgent:
    """Bedrock 에이전트 싱글톤 반환 (비동기)"""
    global _agent
    if _agent is None:
        _agent = BedrockAgent()
    await _agent._ensure_initialized()
    return _agent
