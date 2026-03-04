---
name: code-generation
description: 프로젝트 컨벤션에 맞는 코드 생성 패턴과 가이드를 제공합니다.
inclusion: manual
---

# 코드 생성 스킬 (Code Generation Skill)

코드 생성 시 참고하는 프로젝트별 컨벤션과 패턴입니다.

## 백엔드 컨벤션 (Python/FastAPI)

### 파일 구조
- 엔트리포인트: `backend/src/main.py`
- 설정: `backend/src/config.py`
- 각 모듈은 `backend/src/` 하위에 단일 파일로 구성
- `__init__.py`로 패키지 초기화

### 코딩 패턴
- 비동기 함수(async def)를 기본으로 사용
- Pydantic BaseModel로 데이터 스키마 정의
- 환경 변수는 `config.py`를 통해 관리
- 의존성 주입(Dependency Injection)은 FastAPI의 `Depends` 활용
- 로깅은 Python 표준 `logging` 모듈 사용

### 에러 처리
```python
from fastapi import HTTPException

# 클라이언트 에러
raise HTTPException(status_code=400, detail="잘못된 요청입니다")

# 인증 에러
raise HTTPException(status_code=401, detail="인증이 필요합니다")

# 서버 에러는 try-except로 감싸서 처리
try:
    result = await some_operation()
except Exception as e:
    logger.error(f"작업 실패: {e}")
    raise HTTPException(status_code=500, detail="내부 서버 오류")
```

### 새 API 엔드포인트 추가 시
1. Pydantic 요청/응답 모델 정의
2. 라우터 함수 작성 (async def)
3. 타입 힌트 필수
4. docstring 작성 (Google 스타일)

## 프론트엔드 컨벤션 (Next.js/TypeScript)

### 파일 구조
- App Router: `frontend/src/app/`
- 컴포넌트: `frontend/src/app/components/`
- 글로벌 스타일: `frontend/src/app/globals.css`
