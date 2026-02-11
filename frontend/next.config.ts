import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // 폐쇄망 환경: 이미지 최적화 비활성화 (외부 sharp 의존성 제거)
  images: {
    unoptimized: true,
  },
  // 프록시 타임아웃 확장 (기본 30초 → 5분)
  experimental: {
    proxyTimeout: 300_000,
  },
  // 백엔드 API 프록시 설정
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.BACKEND_URL || "http://backend:8000"}/api/:path*`,
      },
      {
        source: "/health",
        destination: `${process.env.BACKEND_URL || "http://backend:8000"}/health`,
      },
    ];
  },
};

export default nextConfig;
