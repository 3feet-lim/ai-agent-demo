# Frontend Dockerfile - 폐쇄망 환경 대응
# nginx로 정적 파일 서빙, 외부 CDN 의존성 없음

FROM nginx:1.25-alpine

# 기본 nginx 설정 제거
RUN rm /etc/nginx/conf.d/default.conf

# 커스텀 nginx 설정 복사
COPY nginx.conf /etc/nginx/conf.d/

# 정적 파일 복사 (모든 CSS/JS가 로컬에 포함되어야 함)
COPY static/ /usr/share/nginx/html/

# 비특권 사용자로 실행하기 위한 설정
RUN chown -R nginx:nginx /usr/share/nginx/html && \
    chown -R nginx:nginx /var/cache/nginx && \
    chown -R nginx:nginx /var/log/nginx && \
    touch /var/run/nginx.pid && \
    chown -R nginx:nginx /var/run/nginx.pid

EXPOSE 80

# 헬스체크
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:80/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
