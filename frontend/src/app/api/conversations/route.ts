import { NextRequest } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://backend:8000";

/**
 * 대화 목록 조회 프록시
 */
export async function GET(request: NextRequest) {
  const userId = request.headers.get("x-user-id") || "";

  const res = await fetch(`${BACKEND_URL}/api/conversations`, {
    headers: { "X-User-Id": userId },
  });

  const data = await res.text();
  return new Response(data, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
