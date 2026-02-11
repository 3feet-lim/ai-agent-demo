"use client";

import { useState, useCallback, useEffect } from "react";
import Sidebar from "./components/Sidebar";
import ChatArea from "./components/ChatArea";
import MessageInput from "./components/MessageInput";
import ErrorBoundary from "./components/ErrorBoundary";

const API_BASE = "/api";

interface Message {
  role: string;
  content: string;
}

interface Conversation {
  id: string;
  title: string | null;
  updated_at: string;
  preview: string | null;
}

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  // 대화 목록 로드
  const loadConversations = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/conversations`);
      if (res.ok) {
        setConversations(await res.json());
      }
    } catch (err) {
      console.error("대화 목록 로드 실패:", err);
    }
  }, []);

  useEffect(() => {
    loadConversations();
  }, [loadConversations]);

  // 특정 대화 로드
  const handleSelectConversation = useCallback(async (id: string) => {
    try {
      const res = await fetch(`${API_BASE}/conversations/${id}`);
      if (!res.ok) return;
      const data = await res.json();
      setCurrentId(id);
      setMessages(
        data.messages?.map((m: Message) => ({
          role: m.role,
          content: m.content,
        })) || []
      );
    } catch (err) {
      console.error("대화 로드 실패:", err);
    }
  }, []);

  // 새 대화
  const handleNewChat = useCallback(() => {
    setCurrentId(null);
    setMessages([]);
  }, []);

  // 메시지 전송 (SSE 스트리밍 지원, JSON 폴백)
  const handleSend = useCallback(
    async (content: string) => {
      setMessages((prev) => [...prev, { role: "user", content }]);
      setIsLoading(true);

      try {
        // 도구 호출이 여러 번 반복될 수 있으므로 타임아웃을 5분으로 설정
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 300_000);

        const res = await fetch(`${API_BASE}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message: content,
            conversation_id: currentId,
          }),
          signal: controller.signal,
        });

        clearTimeout(timeoutId);

        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        const contentType = res.headers.get("content-type") || "";

        // SSE 스트리밍 응답 처리
        if (contentType.includes("text/event-stream") && res.body) {
          // 스트리밍 중 빈 assistant 메시지 추가
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: "" },
          ]);

          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          let convId = currentId;

          while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop() || "";

            for (const line of lines) {
              if (!line.startsWith("data: ")) continue;
              const data = line.slice(6);
              if (data === "[DONE]") continue;

              try {
                const parsed = JSON.parse(data);

                if (parsed.conversation_id && !convId) {
                  convId = parsed.conversation_id;
                  setCurrentId(convId);
                }

                if (parsed.token) {
                  // 마지막 assistant 메시지에 토큰 추가
                  setMessages((prev) => {
                    const updated = [...prev];
                    const last = updated[updated.length - 1];
                    if (last?.role === "assistant") {
                      updated[updated.length - 1] = {
                        ...last,
                        content: last.content + parsed.token,
                      };
                    }
                    return updated;
                  });
                }
              } catch {
                // JSON 파싱 실패 시 무시
              }
            }
          }
        } else {
          // 일반 JSON 응답 (현재 backend 호환)
          const data = await res.json();

          if (data.conversation_id) {
            setCurrentId(data.conversation_id);
          }

          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: data.response },
          ]);
        }

        loadConversations();
      } catch (err) {
        const isTimeout =
          err instanceof DOMException && err.name === "AbortError";
        console.error("메시지 전송 실패:", err);
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: isTimeout
              ? "응답 시간이 초과되었습니다. 질문을 더 구체적으로 해주시거나 다시 시도해주세요."
              : "메시지 전송에 실패했습니다. 다시 시도해주세요.",
          },
        ]);
      } finally {
        setIsLoading(false);
      }
    },
    [currentId, loadConversations]
  );

  return (
    <div className="app-container">
      <Sidebar
        conversations={conversations}
        currentId={currentId}
        onNewChat={handleNewChat}
        onSelect={handleSelectConversation}
      />
      <main className="chat-container">
        <header className="chat-header">
          <h1>AI Agent Demo</h1>
          <span className="model-badge">Claude Sonnet</span>
        </header>
        <ErrorBoundary>
          <ChatArea messages={messages} isLoading={isLoading} />
        </ErrorBoundary>
        <MessageInput onSend={handleSend} disabled={isLoading} />
      </main>
    </div>
  );
}
