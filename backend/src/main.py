"""
AI Agent Demo - FastAPI 백엔드
LangChain + LangGraph + Bedrock 기반
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .config import get_settings
from .bedrock_client import get_bedrock_agent
from .conversation_store import get_conversation_store
from .mcp_manager import get_mcp_manager

# 로깅 설정
settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


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

            # conversation_id 먼저 전송
            yield f"data: {json.dumps({'conversation_id': conversation_id})}\n\n"

            try:
                async for token in agent.chat_stream(
                    request.message, history, conversation_id
                ):
                    full_response.append(token)
                    yield f"data: {json.dumps({'token': token})}\n\n"

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
