# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

웹 채팅 UI와 AWS Bedrock (Claude)을 통합한 AI 에이전트 데모 프로젝트. MCP (Model Context Protocol)를 사용하여 컨텍스트를 수집하고 응답합니다. 프론트엔드와 백엔드가 분리된 Docker 기반 구조입니다.

## Commands

### Docker로 실행 (권장)
```bash
cp .env.example .env  # 환경 변수 설정
docker-compose up --build
```

### 로컬 개발 (백엔드)
```bash
cd backend
pip install -r requirements.txt
uvicorn src.main:app --reload
```

### 테스트
```bash
cd backend
pytest tests/
```

## Project Structure

```
ai-agent-demo/
├── frontend/                # 프론트엔드 (nginx)
│   ├── Dockerfile
│   ├── nginx.conf
│   └── static/
│       ├── index.html
│       ├── style.css
│       └── app.js
├── backend/                 # 백엔드 (FastAPI)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── src/
│       ├── main.py
│       ├── bedrock_client.py
│       ├── mcp_manager.py
│       ├── conversation_store.py
│       └── config.py
├── docker-compose.yml
└── .env.example
```

## Architecture

```
Browser → Nginx (frontend:80) → FastAPI (backend:8000) → Bedrock (Claude)
                                       ↓
                                MCP Manager + Conversation Store
```

- **Frontend**: nginx로 정적 파일 서빙, API 요청은 백엔드로 프록시
- **Backend**: FastAPI 서버, AWS Bedrock 연동
- **MCP Manager**: 컨텍스트 수집
- **Conversation Store**: SQLite 기반 대화 히스토리

## Key Technologies

- **Frontend**: nginx, HTML/CSS/JavaScript
- **Backend**: FastAPI, uvicorn, Python
- **AI**: AWS Bedrock (Claude Sonnet 4.5)
- **Protocol**: MCP (Model Context Protocol)
- **Infra**: Docker, docker-compose

## Environment Variables

`.env` 파일에 설정:
- `AWS_REGION`: AWS 리전 (기본값: ap-northeast-2)
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`: AWS 자격 증명
- `BEDROCK_MODEL_ID`: 모델 ID (기본값: anthropic.claude-sonnet-4-5-v2:0)
- `MCP_SERVER_URL`: MCP 서버 URL (선택 사항)

---

## 개발 진행 상황

### 완료된 작업 (2026-01-21)

#### 1. Docker 설정 (폐쇄망 환경 대응)
- [x] `backend/Dockerfile` - Python 3.11-slim, 모든 의존성 빌드 시점 설치
- [x] `frontend/Dockerfile` - nginx:1.25-alpine, 정적 파일 서빙
- [x] `docker-compose.yml` - 서비스 오케스트레이션
- [x] `frontend/nginx.conf` - API 프록시 설정

**폐쇄망 배포 방법:**
```bash
# 외부 환경에서 빌드
docker-compose build
docker save ai-agent-frontend ai-agent-backend -o images.tar

# 폐쇄망에서 로드
docker load -i images.tar
docker-compose up
```

#### 2. Frontend (Vanilla JS + 다크 모드)
- [x] `frontend/static/index.html` - 채팅 UI 구조
- [x] `frontend/static/style.css` - 다크 모드 스타일 (GitHub 스타일)
- [x] `frontend/static/app.js` - 채팅 로직, API 연동

**기능:**
- 사이드바 대화 목록
- 메시지 버블 (사용자/AI)
- 타이핑 인디케이터
- 코드 블록 렌더링
- Enter 전송, Shift+Enter 줄바꿈

#### 3. Backend (LangChain + LangGraph + Bedrock)
- [x] `backend/src/config.py` - Pydantic Settings 환경 설정
- [x] `backend/src/conversation_store.py` - SQLite 비동기 대화 저장소
- [x] `backend/src/mcp_manager.py` - MCP 연동 관리자
- [x] `backend/src/bedrock_client.py` - LangChain + LangGraph 에이전트
- [x] `backend/src/main.py` - FastAPI 엔드포인트

**API 엔드포인트:**
| Method | Path | 설명 |
|--------|------|------|
| GET | `/health` | 헬스체크 |
| GET | `/api/status` | 상태 확인 |
| POST | `/api/chat` | 채팅 메시지 전송 |
| GET | `/api/conversations` | 대화 목록 |
| GET | `/api/conversations/{id}` | 대화 상세 |
| DELETE | `/api/conversations/{id}` | 대화 삭제 |

#### 4. 테스트 (58개 테스트 통과)
- [x] `backend/tests/conftest.py` - 공통 fixture
- [x] `backend/tests/test_config.py` - 설정 테스트
- [x] `backend/tests/test_conversation_store.py` - 대화 저장소 테스트
- [x] `backend/tests/test_mcp_manager.py` - MCP 관리자 테스트
- [x] `backend/tests/test_bedrock_client.py` - Bedrock 클라이언트 테스트
- [x] `backend/tests/test_api.py` - FastAPI 엔드포인트 테스트

**테스트 실행:**
```bash
source env/bin/activate
cd backend
pytest -v
```

---

### 완료된 추가 작업 (2026-01-22)

#### 5. MCP 서버 설정
- [x] Grafana MCP (`mcp-grafana`) - uvx로 독립 실행
- [x] CloudWatch MCP (`awslabs-cloudwatch-mcp-server`) - uvx로 독립 실행
- [x] MCP Manager 업데이트 - 서버 연결 및 도구 관리

**MCP 환경 변수:**
```bash
# Grafana MCP
GRAFANA_URL=https://your-grafana-instance.com
GRAFANA_API_KEY=your-grafana-api-key

# CloudWatch MCP (AWS 자격 증명 사용)
AWS_REGION=ap-northeast-2
```

**참고:** MCP 서버는 uvx를 통해 독립적인 환경에서 실행됩니다 (의존성 충돌 방지).

#### 6. Docker 빌드 및 통합 테스트
- [x] Docker 이미지 빌드 성공
- [x] 컨테이너 실행 테스트 통과
- [x] API 엔드포인트 테스트 통과
- [x] Bedrock 연동 테스트 통과

---

### 다음 작업 (TODO)

1. **MCP 클라이언트 연동 개선** - 비동기 컨텍스트 관리 수정 필요
2. **MCP Tool 실제 테스트** - Grafana/CloudWatch 설정 후 도구 호출 테스트

---

### 주요 설정 정보

| 항목 | 값 |
|------|-----|
| AWS 리전 | ap-northeast-2 |
| Bedrock 모델 | anthropic.claude-sonnet-4-5-v2:0 |
| Agent 프레임워크 | LangChain + LangGraph |
| 대화 저장소 | SQLite (aiosqlite) |
| 프론트엔드 스타일 | 다크 모드 (Vanilla JS + CSS) |
| MCP 서버 | Grafana MCP, CloudWatch MCP |
| MCP 실행 방식 | uvx (독립 환경) |
