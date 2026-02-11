"""
SQLite 기반 대화 저장소
"""
import aiosqlite
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import get_settings


class ConversationStore:
    """비동기 대화 저장소"""

    def __init__(self, db_path: Optional[str] = None):
        settings = get_settings()
        self.db_path = db_path or settings.conversation_db_path
        self._ensure_directory()

    def _ensure_directory(self):
        """DB 디렉토리 생성"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def initialize(self):
        """데이터베이스 테이블 초기화"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    title TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            # 기존 DB 마이그레이션: user_id 컬럼이 없으면 추가
            try:
                await db.execute("ALTER TABLE conversations ADD COLUMN user_id TEXT")
            except Exception:
                pass  # 이미 존재하면 무시
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_user
                ON conversations(user_id)
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    role TEXT,
                    content TEXT,
                    created_at TEXT,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(conversation_id)
            """)
            # 대화 요약 테이블 (하이브리드 메모리용)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    conversation_id TEXT PRIMARY KEY,
                    summary TEXT,
                    summarized_until INTEGER DEFAULT 0,
                    updated_at TEXT,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                )
            """)
            await db.commit()

    async def create_conversation(self, title: Optional[str] = None, user_id: Optional[str] = None) -> str:
        """새 대화 생성"""
        conversation_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, user_id, title, now, now)
            )
            await db.commit()

        return conversation_id

    async def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str
    ) -> str:
        """메시지 추가"""
        message_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (message_id, conversation_id, role, content, now)
            )
            await db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id)
            )

            # 첫 메시지면 제목 자동 설정
            cursor = await db.execute(
                "SELECT title FROM conversations WHERE id = ?",
                (conversation_id,)
            )
            row = await cursor.fetchone()
            if row and row[0] is None and role == "user":
                title = content[:50] + ("..." if len(content) > 50 else "")
                await db.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?",
                    (title, conversation_id)
                )

            await db.commit()

        return message_id

    async def get_conversation(self, conversation_id: str) -> Optional[dict]:
        """대화 상세 조회"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,)
            )
            conv_row = await cursor.fetchone()
            if not conv_row:
                return None

            cursor = await db.execute(
                "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at",
                (conversation_id,)
            )
            messages = await cursor.fetchall()

            return {
                "id": conv_row["id"],
                "title": conv_row["title"],
                "created_at": conv_row["created_at"],
                "updated_at": conv_row["updated_at"],
                "messages": [
                    {"role": m["role"], "content": m["content"], "created_at": m["created_at"]}
                    for m in messages
                ]
            }

    async def get_messages(self, conversation_id: str) -> list[dict]:
        """대화의 메시지 목록 조회 (LangChain 형식)"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at",
                (conversation_id,)
            )
            messages = await cursor.fetchall()
            return [{"role": m["role"], "content": m["content"]} for m in messages]

    async def list_conversations(self, limit: int = 50, user_id: Optional[str] = None) -> list[dict]:
        """대화 목록 조회 (user_id 기반 필터링)"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if user_id:
                cursor = await db.execute(
                    """
                    SELECT c.id, c.title, c.updated_at,
                           (SELECT content FROM messages WHERE conversation_id = c.id ORDER BY created_at LIMIT 1) as preview
                    FROM conversations c
                    WHERE c.user_id = ?
                    ORDER BY c.updated_at DESC
                    LIMIT ?
                    """,
                    (user_id, limit,)
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT c.id, c.title, c.updated_at,
                           (SELECT content FROM messages WHERE conversation_id = c.id ORDER BY created_at LIMIT 1) as preview
                    FROM conversations c
                    ORDER BY c.updated_at DESC
                    LIMIT ?
                    """,
                    (limit,)
                )
            rows = await cursor.fetchall()
            return [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "updated_at": r["updated_at"],
                    "preview": r["preview"][:50] if r["preview"] else None
                }
                for r in rows
            ]

    async def delete_conversation(self, conversation_id: str) -> bool:
        """대화 삭제"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM messages WHERE conversation_id = ?",
                (conversation_id,)
            )
            await db.execute(
                "DELETE FROM conversation_summaries WHERE conversation_id = ?",
                (conversation_id,)
            )
            cursor = await db.execute(
                "DELETE FROM conversations WHERE id = ?",
                (conversation_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_summary(self, conversation_id: str) -> Optional[dict]:
        """대화 요약 조회"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT summary, summarized_until FROM conversation_summaries WHERE conversation_id = ?",
                (conversation_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "summary": row["summary"],
                "summarized_until": row["summarized_until"],
            }

    async def save_summary(
        self,
        conversation_id: str,
        summary: str,
        summarized_until: int,
    ) -> None:
        """대화 요약 저장 (upsert)"""
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO conversation_summaries (conversation_id, summary, summarized_until, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id)
                DO UPDATE SET summary = excluded.summary,
                              summarized_until = excluded.summarized_until,
                              updated_at = excluded.updated_at
                """,
                (conversation_id, summary, summarized_until, now)
            )
            await db.commit()

    async def get_message_count(self, conversation_id: str) -> int:
        """대화의 총 메시지 수 조회"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation_id,)
            )
            row = await cursor.fetchone()
            return row[0] if row else 0


# 싱글톤 인스턴스
_store: Optional[ConversationStore] = None


async def get_conversation_store() -> ConversationStore:
    """대화 저장소 싱글톤 반환"""
    global _store
    if _store is None:
        _store = ConversationStore()
        await _store.initialize()
    return _store
