import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // 폐쇄망 환경: 이미지 최적화 비활성화 (외부 sharp 의존성 제거)
  images: {
    unoptimized: true,
  },
  // /health 엔드포인트만 백엔드로 프록시
  // /api/* 경로는 Route Handler에서 직접 처리 (SSE 스트리밍 버퍼링 방지)
  async rewrites() {
    return [
      {
        source: "/health",
        destination: `${process.env.BACKEND_URL || "http://backend:8000"}/health`,
      },
    ];
  },
};

export default nextConfig;
