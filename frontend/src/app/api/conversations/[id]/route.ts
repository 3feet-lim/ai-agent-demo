import { NextRequest } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://backend:8000";

/**
 * 특정 대화 조회/삭제 프록시
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const userId = request.headers.get("x-user-id") || "";

  const res = await fetch(`${BACKEND_URL}/api/conversations/${id}`, {
    headers: { "X-User-Id": userId },
  });

  const data = await res.text();
  return new Response(data, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const userId = request.headers.get("x-user-id") || "";

  const res = await fetch(`${BACKEND_URL}/api/conversations/${id}`, {
    method: "DELETE",
    headers: { "X-User-Id": userId },
  });

  const data = await res.text();
  return new Response(data, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
