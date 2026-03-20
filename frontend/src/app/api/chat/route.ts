import { NextRequest } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://backend:8000";

// Next.js Route Handler 실행 시간 제한 해제 (SSE 스트리밍은 장시간 연결 유지 필요)
export const maxDuration = 300;

/**
 * SSE 스트리밍 패스스루 프록시
 * Next.js rewrites는 SSE 응답을 버퍼링하므로,
 * Route Handler에서 직접 ReadableStream으로 전달하여 실시간 스트리밍을 구현
 *
 * AbortSignal.timeout은 SSE 스트리밍과 호환되지 않음:
 * - timeout은 fetch 시작 시점부터 절대 시간으로 동작
 * - heartbeat가 와도 타이머가 리셋되지 않아 장시간 스트리밍 시 강제 종료됨
 * - 대신 백엔드 heartbeat(15초 간격)에 의존하여 연결 유지
 */
export async function POST(request: NextRequest) {
  const body = await request.text();
  const userId = request.headers.get("x-user-id") || "";

  const backendRes = await fetch(`${BACKEND_URL}/api/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-User-Id": userId,
    },
    body,
    // AbortSignal.timeout 제거: SSE 스트리밍은 백엔드 heartbeat로 연결 유지
  });

  if (!backendRes.ok) {
    return new Response(backendRes.statusText, { status: backendRes.status });
  }

  // 백엔드 SSE 스트림을 그대로 패스스루
  const stream = backendRes.body;
  if (!stream) {
    return new Response("No response body", { status: 502 });
  }

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "Connection": "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}
