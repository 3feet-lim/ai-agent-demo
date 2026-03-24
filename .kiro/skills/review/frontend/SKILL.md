---
name: frontend-review
description: 프론트엔드(HTML/CSS/JavaScript) 코드 리뷰 체크리스트. 접근성, DOM 조작, 이벤트 처리, CSS 품질 관점의 리뷰 기준을 제공합니다.
---

# 프론트엔드 코드 리뷰 체크리스트

## 심각도 분류
| 심각도 | 아이콘 | 기준 |
|--------|--------|------|
| Critical | 🔴 | 즉시 수정 필요 (XSS, 보안 취약점) |
| Warning | 🟠 | 수정 권장 (성능 이슈, 미처리 에러) |
| Suggestion | 🟡 | 개선 권장 (가독성, 구조 개선) |
| Info | 🔵 | 참고 사항 (대안 제시) |

## 접근성 (Accessibility)
- [ ] 시맨틱 HTML 태그 사용
- [ ] alt 텍스트, aria-label 적용
- [ ] 키보드 네비게이션 지원
- [ ] 색상 대비(contrast) 충분한가

## JavaScript
- [ ] XSS 방지 (innerHTML 대신 textContent 사용)
- [ ] 에러 처리 (fetch 실패 등)
- [ ] 메모리 누수 (이벤트 리스너 정리)
- [ ] null/undefined 처리

## CSS
- [ ] 불필요한 중복 스타일
- [ ] 반응형 디자인 적용
- [ ] CSS 변수 활용
