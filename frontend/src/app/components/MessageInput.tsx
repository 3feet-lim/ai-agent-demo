"use client";

import { useRef, useCallback, KeyboardEvent, ChangeEvent } from "react";

interface MessageInputProps {
  onSend: (message: string) => void;
  disabled: boolean;
}

export default function MessageInput({ onSend, disabled }: MessageInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // textarea 높이 자동 조절
  const handleChange = useCallback((e: ChangeEvent<HTMLTextAreaElement>) => {
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 150) + "px";
  }, []);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        const value = textareaRef.current?.value.trim();
        if (value && !disabled) {
          onSend(value);
          if (textareaRef.current) {
            textareaRef.current.value = "";
            textareaRef.current.style.height = "auto";
          }
        }
      }
    },
    [onSend, disabled]
  );

  const handleClick = useCallback(() => {
    const value = textareaRef.current?.value.trim();
    if (value && !disabled) {
      onSend(value);
      if (textareaRef.current) {
        textareaRef.current.value = "";
        textareaRef.current.style.height = "auto";
      }
    }
  }, [onSend, disabled]);

  return (
    <div className="input-container">
      <div className="input-wrapper">
        <textarea
          ref={textareaRef}
          className="message-input"
          placeholder="메시지를 입력하세요..."
          rows={1}
          autoFocus
          onChange={handleChange}
          onKeyDown={handleKeyDown}
        />
        <button
          className="send-btn"
          disabled={disabled}
          onClick={handleClick}
          aria-label="메시지 전송"
        >
          <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
            <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" />
          </svg>
        </button>
      </div>
      <p className="input-hint">Enter로 전송, Shift+Enter로 줄바꿈</p>
    </div>
  );
}
