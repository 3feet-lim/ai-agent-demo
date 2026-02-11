"use client";

import { useEffect, useRef } from "react";

interface Message {
  role: string;
  content: string;
}

interface ChatAreaProps {
  messages: Message[];
  isLoading: boolean;
}

// 메시지 포맷팅 (마크다운 기본 지원)
function formatMessage(content: string): string {
  // XSS 방지
  let escaped = content
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  // 코드 블록 (```...```)
  escaped = escaped.replace(
    /```(\w*)\n?([\s\S]*?)```/g,
    (_match, lang, code) =>
      `<pre><code class="language-${lang}">${code.trim()}</code></pre>`
  );

  // 인라인 코드 (`...`)
  escaped = escaped.replace(/`([^`]+)`/g, "<code>$1</code>");

  // 줄바꿈
  escaped = escaped.replace(/\n/g, "<br>");

  return escaped;
}

export default function ChatArea({ messages, isLoading }: ChatAreaProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  // 새 메시지 시 스크롤 하단 이동
  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [messages, isLoading]);

  return (
    <div className="messages-container" ref={containerRef}>
      {messages.length === 0 && !isLoading && (
        <div className="welcome-message">
          <h2>무엇을 도와드릴까요?</h2>
          <p>질문을 입력하시면 AI가 답변해 드립니다.</p>
        </div>
      )}

      {messages.map((msg, idx) => (
        <div key={idx} className={`message ${msg.role}`}>
          <div className="message-avatar">
            {msg.role === "user" ? "U" : "AI"}
          </div>
          <div
            className="message-content"
            dangerouslySetInnerHTML={{ __html: formatMessage(msg.content) }}
          />
        </div>
      ))}

      {isLoading && (
        <div className="message assistant">
          <div className="message-avatar">AI</div>
          <div className="message-content">
            <div className="typing-indicator">
              <span />
              <span />
              <span />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
