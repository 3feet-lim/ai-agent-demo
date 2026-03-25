"""
AI Agent Demo - FastAPI 백엔드
LangChain + LangGraph + Bedrock 기반
"""
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from .config import get_settings
from .bedrock_client import get_bedrock_agent
from .conversation_store import get_conversation_store
from .mcp_manager import get_mcp_manager
from .webhook_handler import (
    format_alertmanager_payload, send_to_slack,
    is_duplicate_alert, mark_alert_processed,
    _get_alert_queue, start_alert_workers, stop_alert_workers,
)

# loguru 설정
settings = get_settings()
logger.remove()  # 기본 핸들러 제거
logger.add(
    sys.stderr,
    level=settings.log_level,
    format="{time:YYYY-MM-DD HH:mm:ss} - {name} - {level} - {message}",
)


async def _init_mcp_background():
    """백그라운드에서 MCP 서버 연결 (uvicorn lifespan 타임아웃 회피)"""
    try:
        await get_mcp_manager()
        logger.info("MCP manager initialized")
    except BaseException as e:
        logger.warning(f"MCP manager initialization failed, but continuing: {type(e).__name__}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """애플리케이션 라이프사이클 관리"""
    # 시작 시 초기화
    logger.info("Starting P리전 장애/이슈 분석 에이전트...")
    await get_conversation_store()
    
    # MCP 연결을 백그라운드 태스크로 실행
    # uvicorn lifespan 타임아웃에 영향받지 않도록 분리
    mcp_task = asyncio.create_task(_init_mcp_background())
    
    logger.info(f"Using Bedrock model: {settings.bedrock_model_id}")
    logger.info(f"AWS Region: {settings.aws_region}")
    if not settings.slack_webhook_url:
        logger.warning("[Slack] SLACK_WEBHOOK_URL이 설정되지 않았습니다. Webhook 알람 분석 결과가 Slack으로 전송되지 않습니다.")

    # 알람 분석 워커 시작 (동시 3개)
    start_alert_workers()

    yield
    # 종료 시 정리
    logger.info("Shutting down P리전 장애/이슈 분석 에이전트...")

    # 알람 분석 워커 종료
    await stop_alert_workers()
    # 아직 MCP 초기화 중이면 완료 대기
    if not mcp_task.done():
        mcp_task.cancel()
        try:
            await mcp_task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        mcp = await get_mcp_manager()
        await mcp.disconnect()
    except Exception as e:
        logger.warning(f"Error during MCP cleanup: {e}")


app = FastAPI(
    title="P리전 장애/이슈 분석 에이전트",
    description="LangChain + LangGraph + AWS Bedrock 기반 Observability AI 에이전트",
    version="1.0.0",
    lifespan=lifespan
)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request/Response 모델
class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    images: Optional[list[str]] = None  # base64 인코딩된 이미지 리스트


class ChatResponse(BaseModel):
    response: str
    conversation_id: str


class ConversationSummary(BaseModel):
    id: str
    title: Optional[str]
    updated_at: str
    preview: Optional[str]


class Message(BaseModel):
    role: str
    content: str
    created_at: Optional[str] = None


class ConversationDetail(BaseModel):
    id: str
    title: Optional[str]
    created_at: str
    updated_at: str
    messages: list[Message]


# 엔드포인트
@app.get("/health")
async def health_check():
    """헬스체크"""
    return {"status": "healthy"}


@app.get("/api/status")
async def get_status():
    """API 상태 확인"""
    mcp = await get_mcp_manager()
    return {
        "status": "ok",
        "model": settings.bedrock_model_id,
        "region": settings.aws_region,
        "mcp_enabled": mcp.is_enabled,
        "mcp_servers": mcp.get_available_servers(),
        "mcp_tools": mcp.get_available_tools()
    }


@app.post("/api/chat")
async def chat(request: ChatRequest, x_user_id: Optional[str] = Header(None)):
    """채팅 메시지 처리 (SSE 스트리밍)"""
    try:
        store = await get_conversation_store()
        agent = await get_bedrock_agent()

        # 새 대화 또는 기존 대화
        conversation_id = request.conversation_id
        if not conversation_id:
            conversation_id = await store.create_conversation(user_id=x_user_id)

        # 기존 메시지 히스토리 로드
        history = await store.get_messages(conversation_id)

        async def event_generator():
            """SSE 이벤트 생성기"""
            full_response = []
            tool_trace = []  # 도구 호출 순서 기록

            # conversation_id 먼저 전송
            yield f"data: {json.dumps({'conversation_id': conversation_id})}\n\n"

            try:
                async for event in agent.chat_stream(
                    request.message, history, conversation_id,
                    images=request.images,
                ):
                    event_type = event.get("type")

                    if event_type == "phase":
                        yield f"data: {json.dumps({'phase': event['message']})}\n\n"

                    elif event_type == "tool_start":
                        yield f"data: {json.dumps({'tool_start': event['name'], 'tool_start_display': event.get('display', event['name'])})}\n\n"

                    elif event_type == "tool_end":
                        yield f"data: {json.dumps({'tool_end': event['name'], 'tool_end_display': event.get('display', event['name'])})}\n\n"
                        # 성공한 도구만 trace에 기록
                        if event.get("success", True):
                            tool_trace.append(event["name"])

                    elif event_type == "mcp_tool_start":
                        yield f"data: {json.dumps({'mcp_tool_start': event.get('display', event['name'])})}\n\n"

                    elif event_type == "mcp_tool_end":
                        yield f"data: {json.dumps({'mcp_tool_end': event.get('display', event['name']), 'mcp_tool_success': event.get('success', True)})}\n\n"

                    elif event_type == "execution_plan":
                        yield f"data: {json.dumps({'execution_plan': event['plan']})}\n\n"

                    elif event_type == "token":
                        token = event["content"]
                        full_response.append(token)
                        yield f"data: {json.dumps({'token': token})}\n\n"

                # 도구 호출 이력을 마지막에 전송 (호출 횟수 포함, 성공한 도구만)
                if tool_trace:
                    from collections import Counter
                    counts = Counter(tool_trace)
                    # 첫 등장 순서 유지 + 횟수 표시
                    seen = set()
                    display_trace = []
                    for t in tool_trace:
                        if t not in seen:
                            seen.add(t)
                            count = counts[t]
                            display_trace.append(
                                f"{t} (x{count})" if count > 1 else t
                            )
                    yield f"data: {json.dumps({'tool_trace': display_trace})}\n\n"

                # 스트리밍 완료 후 메시지 저장
                response_text = "".join(full_response)
                if response_text:
                    await store.add_message(conversation_id, "user", request.message)
                    await store.add_message(conversation_id, "assistant", response_text)

            except Exception as e:
                logger.error(f"Streaming error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

                # 에러 발생 시에도 부분 응답이 있으면 저장
                response_text = "".join(full_response)
                if response_text:
                    await store.add_message(conversation_id, "user", request.message)
                    await store.add_message(
                        conversation_id, "assistant",
                        response_text + "\n\n⚠️ 응답 생성 중 오류가 발생하여 중단되었습니다."
                    )

            yield "data: [DONE]\n\n"

        # StreamingResponse에 keepalive 용 heartbeat를 주입하는 래퍼
        async def event_generator_with_heartbeat():
            """SSE 이벤트를 전달하면서, 장시간 무응답 시 heartbeat(SSE 주석)를 전송.
            
            주의: asyncio.wait_for는 타임아웃 시 내부 코루틴을 cancel하므로
            async generator의 __anext__()에 직접 사용하면 generator 상태가 깨질 수 있다.
            대신 asyncio.Event 기반으로 generator를 별도 태스크에서 소비하여
            cancel 없이 heartbeat를 전송한다.
            """
            HEARTBEAT_INTERVAL = 15  # 초

            queue: asyncio.Queue = asyncio.Queue()
            done = asyncio.Event()

            async def _producer():
                """event_generator()를 소비하여 큐에 넣는 태스크"""
                try:
                    async for event in event_generator():
                        await queue.put(event)
                except Exception as e:
                    logger.error(f"Heartbeat producer error: {e}")
                    await queue.put(f"data: {json.dumps({'error': str(e)})}\n\n")
                finally:
                    done.set()

            producer_task = asyncio.create_task(_producer())

            try:
                while True:
                    if done.is_set() and queue.empty():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
                        yield event
                    except asyncio.TimeoutError:
                        # 큐에서 대기 중 타임아웃 → heartbeat 전송 (generator에 영향 없음)
                        yield ": heartbeat\n\n"
            finally:
                if not producer_task.done():
                    producer_task.cancel()
                    try:
                        await producer_task
                    except (asyncio.CancelledError, Exception):
                        pass

        return StreamingResponse(
            event_generator_with_heartbeat(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/conversations", response_model=list[ConversationSummary])
async def list_conversations(x_user_id: Optional[str] = Header(None)):
    """대화 목록 조회 (사용자별 필터링)"""
    try:
        store = await get_conversation_store()
        conversations = await store.list_conversations(user_id=x_user_id)
        return conversations
    except Exception as e:
        logger.error(f"Error listing conversations: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: str):
    """대화 상세 조회"""
    try:
        store = await get_conversation_store()
        conversation = await store.get_conversation(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conversation
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting conversation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    """대화 삭제"""
    try:
        store = await get_conversation_store()
        deleted = await store.delete_conversation(conversation_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting conversation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Prometheus Alertmanager Webhook ──────────────────────────────

@app.post("/api/webhook/alertmanager")
async def alertmanager_webhook(request: Request):
    """
    Prometheus Alertmanager webhook 수신 → Agent 분석 → Slack 전송.

    Alertmanager의 webhook_configs에 이 엔드포인트를 등록하면,
    알람 발생 시 자동으로 분석 리포트가 Slack으로 전송됩니다.

    Alertmanager 설정 예시:
      receivers:
        - name: 'ai-agent'
          webhook_configs:
            - url: 'http://backend:8000/api/webhook/alertmanager'
    """
    try:
        payload = await request.json()
        logger.info(f"[Webhook] Alertmanager 알람 수신: status={payload.get('status')}, "
                     f"alerts={len(payload.get('alerts', []))}건")

        # resolved 상태는 분석 건너뜀
        if payload.get("status") == "resolved":
            logger.info("[Webhook] resolved 알람 → 분석 건너뜀")
            return {"status": "skipped", "reason": "resolved"}

        # 중복 알람 체크 (1시간 이내 동일 알람 스킵)
        if is_duplicate_alert(payload):
            logger.info("[Webhook] 중복 알람 → 분석 건너뜀")
            return {"status": "skipped", "reason": "duplicate"}

        # payload를 에이전트가 이해할 수 있는 텍스트로 변환
        alert_message = format_alertmanager_payload(payload)
        logger.info(f"[Webhook] 변환된 알람 메시지:\n{alert_message[:500]}")

        # 큐에 넣고 즉시 응답 (백그라운드 워커가 처리)
        queue = _get_alert_queue()
        await queue.put((alert_message, payload))
        logger.info(f"[Webhook] 분석 큐에 추가 (대기 중: {queue.qsize()}건)")

        return {
            "status": "queued",
            "queue_size": queue.qsize(),
        }

    except Exception as e:
        logger.error(f"[Webhook] 처리 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))
