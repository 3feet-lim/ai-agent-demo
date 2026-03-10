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
from .webhook_handler import format_alertmanager_payload, send_to_slack

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
    logger.info("Starting Olly Agent...")
    await get_conversation_store()
    
    # MCP 연결을 백그라운드 태스크로 실행
    # uvicorn lifespan 타임아웃에 영향받지 않도록 분리
    mcp_task = asyncio.create_task(_init_mcp_background())
    
    logger.info(f"Using Bedrock model: {settings.bedrock_model_id}")
    logger.info(f"AWS Region: {settings.aws_region}")
    yield
    # 종료 시 정리
    logger.info("Shutting down Olly Agent...")
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
    title="Olly Agent",
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

                    if event_type == "tool_start":
                        yield f"data: {json.dumps({'tool_start': event['name']})}\n\n"

                    elif event_type == "tool_end":
                        yield f"data: {json.dumps({'tool_end': event['name']})}\n\n"
                        # 성공한 도구만 trace에 기록
                        if event.get("success", True):
                            tool_trace.append(event["name"])

                    elif event_type == "token":
                        token = event["content"]
                        full_response.append(token)
                        yield f"data: {json.dumps({'token': token})}\n\n"

                # 도구 호출 이력을 마지막에 전송 (중복 제거, 순서 유지)
                if tool_trace:
                    seen = set()
                    unique_trace = []
                    for t in tool_trace:
                        if t not in seen:
                            seen.add(t)
                            unique_trace.append(t)
                    yield f"data: {json.dumps({'tool_trace': unique_trace})}\n\n"

                # 스트리밍 완료 후 메시지 저장
                response_text = "".join(full_response)
                if response_text:
                    await store.add_message(conversation_id, "user", request.message)
                    await store.add_message(conversation_id, "assistant", response_text)

            except Exception as e:
                logger.error(f"Streaming error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_generator(),
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

        # firing 상태만 분석 (resolved는 무시)
        if payload.get("status") == "resolved":
            logger.info("[Webhook] resolved 알람 → 분석 건너뜀")
            return {"status": "skipped", "reason": "resolved"}

        # payload를 에이전트가 이해할 수 있는 텍스트로 변환
        alert_message = format_alertmanager_payload(payload)
        logger.info(f"[Webhook] 변환된 알람 메시지:\n{alert_message[:500]}")

        # 에이전트로 분석 요청 (비스트리밍)
        agent = await get_bedrock_agent()
        analysis = await agent.chat(alert_message)
        logger.info(f"[Webhook] 분석 완료: {len(analysis)}자")

        # Slack으로 전송
        slack_sent = await send_to_slack(analysis)

        return {
            "status": "processed",
            "analysis_length": len(analysis),
            "slack_sent": slack_sent,
        }

    except Exception as e:
        logger.error(f"[Webhook] 처리 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))
