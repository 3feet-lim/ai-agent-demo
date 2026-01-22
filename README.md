# AI Agent Demo

웹 채팅 UI와 AWS Bedrock (Claude)을 통합한 AI 에이전트 데모 프로젝트입니다.
MCP (Model Context Protocol)를 사용하여 컨텍스트를 수집하고 응답합니다.

## 주요 기능

- **웹 채팅 UI**: 브라우저 기반 실시간 채팅 인터페이스
- **AWS Bedrock**: Claude Sonnet 4.5 모델 사용 (변경 가능)
- **MCP 도구**: 컨텍스트 수집
- **대화 히스토리**: 세션별 대화 내역 저장 및 관리
- **Docker 기반**: 프론트엔드/백엔드 분리된 컨테이너 구조

## 프로젝트 구조

```
ai-agent-demo/
├── frontend/                    # 프론트엔드 (nginx)
│   ├── Dockerfile
│   ├── nginx.conf
│   └── static/
│       ├── index.html           # 채팅 UI
│       ├── style.css            # 스타일시트
│       └── app.js               # 클라이언트 JavaScript
├── backend/                     # 백엔드 (FastAPI)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── src/
│       ├── main.py              # FastAPI 애플리케이션 진입점
│       ├── bedrock_client.py    # AWS Bedrock 클라이언트
│       ├── mcp_manager.py       # MCP 도구 매니저
│       ├── conversation_store.py # 대화 히스토리 관리
│       └── config.py            # 설정 관리
├── tests/                       # 테스트 코드
├── docker-compose.yml           # Docker Compose 설정
├── .env.example                 # 환경 변수 템플릿
└── README.md
```

## 빠른 시작 (Docker)

### 1. 환경 변수 설정

```bash
cp .env.example .env
# .env 파일에서 AWS 자격 증명 설정
```

### 2. Docker Compose로 실행

```bash
docker-compose up --build
```

브라우저에서 `http://localhost` 접속

## 로컬 개발

### 백엔드 개발

```bash
cd backend
pip install -r requirements.txt
uvicorn src.main:app --reload --port 8000
```

### 프론트엔드 개발

프론트엔드는 정적 파일이므로 `frontend/static/` 디렉토리의 파일을 직접 수정합니다.
로컬에서 테스트하려면 간단한 HTTP 서버를 사용:

```bash
cd frontend/static
python -m http.server 3000
```

## 환경 변수

`.env` 파일에 설정:

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `AWS_REGION` | AWS 리전 | `us-east-1` |
| `AWS_ACCESS_KEY_ID` | AWS 액세스 키 | - |
| `AWS_SECRET_ACCESS_KEY` | AWS 시크릿 키 | - |
| `BEDROCK_MODEL_ID` | Bedrock 모델 ID | `anthropic.claude-sonnet-4-5-v2:0` |
| `LOG_LEVEL` | 로그 레벨 | `INFO` |

## AWS 설정

1. AWS 계정에 Bedrock 액세스 권한 설정
2. Claude 모델 액세스 활성화 (Bedrock 콘솔 > Model access에서)
3. `.env` 파일에 AWS 자격 증명 설정

## 테스트

```bash
cd backend
pytest tests/
```

## 아키텍처

```
┌─────────────────┐
│   Web Browser   │
└──────┬──────────┘
       │ HTTP
       v
┌─────────────────┐     ┌─────────────────┐
│    Frontend     │────>│     Backend     │
│  (nginx:80)     │     │  (FastAPI:8000) │
└─────────────────┘     └──────┬──────────┘
                               │
                ┌──────────────┼──────────────┐
                │              │              │
                v              v              v
         ┌───────────┐  ┌───────────┐  ┌───────────┐
         │    MCP    │  │Conversation│  │  Bedrock  │
         │  Manager  │  │   Store   │  │  (Claude) │
         └───────────┘  └───────────┘  └───────────┘
```

## 라이선스

MIT License