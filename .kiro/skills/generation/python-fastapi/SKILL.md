---
name: python-fastapi-generation
description: Python/FastAPI 백엔드 코드 생성 패턴. 라우터, Pydantic 모델, 비동기 패턴, 에러 처리, 의존성 주입 등을 다룹니다.
---

# Python/FastAPI 코드 생성 가이드

## 파일 구조
- 엔트리포인트: `backend/src/main.py`
- 설정: `backend/src/config.py`
- 각 모듈은 `backend/src/` 하위에 단일 파일로 구성

## 코딩 패턴
- 비동기 함수(async def)를 기본으로 사용
- Pydantic BaseModel로 데이터 스키마 정의
- 환경 변수는 `config.py`를 통해 관리
- 의존성 주입은 FastAPI의 `Depends` 활용
- 로깅은 Python 표준 `logging` 모듈 사용
- 타입 힌트 필수

## 에러 처리
```python
from fastapi import HTTPException

# 클라이언트 에러
raise HTTPException(status_code=400, detail="잘못된 요청입니다")

# 서버 에러는 try-except로 감싸서 처리
try:
    result = await some_operation()
except Exception as e:
    logger.error(f"작업 실패: {e}")
    raise HTTPException(status_code=500, detail="내부 서버 오류")
```

## 새 API 엔드포인트 추가 시
1. Pydantic 요청/응답 모델 정의
2. 라우터 함수 작성 (async def)
3. 타입 힌트 필수
4. docstring 작성 (Google 스타일)
