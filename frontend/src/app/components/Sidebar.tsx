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
}

export default function Sidebar({
  conversations,
  currentId,
  onNewChat,
  onSelect,
}: SidebarProps) {
  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        {/* KB 로고: frontend/public/kb-logo.png 파일을 넣어주세요 */}
        <Image
          src="/kb-logo.png"
          alt="KB국민은행"
          width={80}
          height={28}
          className="sidebar-logo"
          priority
        />
        <span className="sidebar-title">AI Agent</span>
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
  );
}
