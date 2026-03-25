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
    <aside className={`sidebar${isOpen ? "" : " collapsed"}`}>
      {isOpen ? (
        <>
          <div className="sidebar-header">
            <Image
              src="/kb-logo.png"
              alt="KB국민은행"
              width={80}
              height={28}
              className="sidebar-logo"
              priority
            />
            <span className="sidebar-title">P리전 장애/이슈 분석 에이전트</span>
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
        </>
      ) : null}
      <button
        className="sidebar-toggle-btn"
        onClick={onToggle}
        aria-label={isOpen ? "사이드바 접기" : "사이드바 펼치기"}
      >
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path
            d={isOpen ? "M9 2L4 7L9 12" : "M5 2L10 7L5 12"}
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
    </aside>
  );
}
