---
name: javascript-frontend-generation
description: HTML/CSS/JavaScript 프론트엔드 코드 생성 패턴. 시맨틱 HTML, CSS 스타일링, 모던 ES6+ JavaScript, 접근성(accessibility)을 다룹니다.
---

# JavaScript/프론트엔드 코드 생성 가이드

## 파일 구조
- `frontend/static/index.html` - 채팅 UI
- `frontend/static/style.css` - 스타일시트
- `frontend/static/app.js` - 클라이언트 JavaScript
- `frontend/nginx.conf` - Nginx 설정

## HTML 규칙
- 시맨틱 태그 사용 (header, main, section, article 등)
- 접근성: alt 텍스트, aria-label, 키보드 네비게이션 지원
- id/class 네이밍은 kebab-case

## CSS 규칙
- 기존 스타일 패턴을 따름
- CSS 변수(custom properties) 활용
- 반응형 디자인 고려

## JavaScript 규칙
- ES6+ 문법 사용 (const/let, 화살표 함수, 템플릿 리터럴)
- fetch API로 백엔드 통신
- async/await 패턴 사용
- DOM 조작 시 querySelector 사용
