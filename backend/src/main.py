"""
AI Agent Demo - FastAPI 백엔드
LangChain + LangGraph + Bedrock 기반
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
    logger.info("Starting AI Agent Demo...")
    await get_conversation_store()
    
    # MCP 연결을 백그라운드 태스크로 실행
    # uvicorn lifespan 타임아웃에 영향받지 않도록 분리
    mcp_task = asyncio.create_task(_init_mcp_background())
    
    logger.info(f"Using Bedrock model: {settings.bedrock_model_id}")
    logger.info(f"AWS Region: {settings.aws_region}")
    yield
    # 종료 시 정리
    logger.info("Shutting down AI Agent Demo...")
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
    title="AI Agent Demo",
    description="LangChain + LangGraph + AWS Bedrock 기반 AI 에이전트",
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


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """채팅 메시지 처리"""
    try:
        store = await get_conversation_store()
        agent = await get_bedrock_agent()

        # 새 대화 또는 기존 대화
        conversation_id = request.conversation_id
        if not conversation_id:
            conversation_id = await store.create_conversation()

        # 기존 메시지 히스토리 로드
        history = await store.get_messages(conversation_id)

        # AI 응답 생성 (저장 전에 먼저 시도)
        response = await agent.chat(request.message, history)

        # 응답 성공 시에만 사용자 메시지와 AI 응답을 함께 저장
        await store.add_message(conversation_id, "user", request.message)
        await store.add_message(conversation_id, "assistant", response)

        return ChatResponse(
            response=response,
            conversation_id=conversation_id
        )

    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/conversations", response_model=list[ConversationSummary])
async def list_conversations():
    """대화 목록 조회"""
    try:
        store = await get_conversation_store()
        conversations = await store.list_conversations()
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
