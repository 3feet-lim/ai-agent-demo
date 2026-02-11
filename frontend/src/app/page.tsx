"use client";

import { useState, useCallback, useEffect } from "react";
import Sidebar from "./components/Sidebar";
import ChatArea from "./components/ChatArea";
import MessageInput from "./components/MessageInput";

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

  // 메시지 전송
  const handleSend = useCallback(
    async (content: string) => {
      // 사용자 메시지 즉시 표시
      setMessages((prev) => [...prev, { role: "user", content }]);
      setIsLoading(true);

      try {
        const res = await fetch(`${API_BASE}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message: content,
            conversation_id: currentId,
          }),
        });

        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        const data = await res.json();

        if (data.conversation_id) {
          setCurrentId(data.conversation_id);
        }

        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: data.response },
        ]);

        // 대화 목록 갱신
        loadConversations();
      } catch (err) {
        console.error("메시지 전송 실패:", err);
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: "메시지 전송에 실패했습니다. 다시 시도해주세요.",
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
        <ChatArea messages={messages} isLoading={isLoading} />
        <MessageInput onSend={handleSend} disabled={isLoading} />
      </main>
    </div>
  );
}
