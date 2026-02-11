"use client";

import Image from "next/image";

interface Conversation {
  id: string;
  title: string | null;
  updated_at: string;
  preview: string | null;
}

interface SidebarProps {
  conversations: Conversation[];
  currentId: string | null;
  onNewChat: () => void;
  onSelect: (id: string) => void;
  isOpen: boolean;
  onToggle: () => void;
}

export default function Sidebar({
  conversations,
  currentId,
  onNewChat,
  onSelect,
  isOpen,
  onToggle,
}: SidebarProps) {
  return (
    <>
      {/* 접힌 상태에서 여는 버튼 */}
      {!isOpen && (
        <button
          className="sidebar-open-btn"
          onClick={onToggle}
          aria-label="사이드바 열기"
        >
          ☰
        </button>
      )}
      <aside className={`sidebar${isOpen ? "" : " collapsed"}`}>
        <div className="sidebar-header">
          <Image
            src="/kb-logo.png"
            alt="KB국민은행"
            width={80}
            height={28}
            className="sidebar-logo"
            priority
          />
          <span className="sidebar-title">Olly Agent</span>
          <button
            className="sidebar-close-btn"
            onClick={onToggle}
            aria-label="사이드바 닫기"
          >
            ✕
          </button>
        </div>
        <button className="new-chat-btn" onClick={onNewChat}>
          + 새 대화
        </button>
        <div className="conversation-list">
          {conversations.map((conv) => (
            <div
              key={conv.id}
              className={`conversation-item${conv.id === currentId ? " active" : ""}`}
              title={conv.title || conv.preview || "새 대화"}
              onClick={() => onSelect(conv.id)}
            >
              {conv.title || conv.preview || "새 대화"}
            </div>
          ))}
        </div>
      </aside>
    </>
  );
}
