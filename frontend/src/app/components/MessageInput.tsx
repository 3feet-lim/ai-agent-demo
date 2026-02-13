"use client";

import { useRef, useCallback, useState, KeyboardEvent, ChangeEvent } from "react";

interface ImagePreview {
  file: File;
  dataUrl: string;
}

interface MessageInputProps {
  onSend: (message: string, images?: string[]) => void;
  disabled: boolean;
}

export default function MessageInput({ onSend, disabled }: MessageInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [images, setImages] = useState<ImagePreview[]>([]);

  // textarea 높이 자동 조절
  const handleChange = useCallback((e: ChangeEvent<HTMLTextAreaElement>) => {
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 150) + "px";
  }, []);

  // 메시지 전송
  const doSend = useCallback(() => {
    const value = textareaRef.current?.value.trim();
    if ((!value && images.length === 0) || disabled) return;

    const imageDataUrls = images.map((img) => img.dataUrl);
    onSend(value || "(이미지 첨부)", imageDataUrls.length > 0 ? imageDataUrls : undefined);

    // 초기화
    setImages([]);
    if (textareaRef.current) {
      textareaRef.current.value = "";
      textareaRef.current.style.height = "auto";
    }
  }, [onSend, disabled, images]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        doSend();
      }
    },
    [doSend]
  );

  // 클립보드 붙여넣기 (Ctrl+V 스크린샷)
  const handlePaste = useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const items = e.clipboardData?.items;
      if (!items) return;

      for (const item of Array.from(items)) {
        if (!item.type.startsWith("image/")) continue;

        e.preventDefault();
        const file = item.getAsFile();
        if (!file) continue;

        const reader = new FileReader();
        reader.onload = () => {
          setImages((prev) => [
            ...prev,
            { file, dataUrl: reader.result as string },
          ]);
        };
        reader.readAsDataURL(file);
      }
    },
    []
  );

  // 파일 선택 처리
  const handleFileSelect = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      const files = e.target.files;
      if (!files) return;

      Array.from(files).forEach((file) => {
        // 이미지 파일만 허용, 10MB 제한
        if (!file.type.startsWith("image/")) return;
        if (file.size > 10 * 1024 * 1024) {
          alert("이미지 크기는 10MB 이하만 가능합니다.");
          return;
        }

        const reader = new FileReader();
        reader.onload = () => {
          setImages((prev) => [
            ...prev,
            { file, dataUrl: reader.result as string },
          ]);
        };
        reader.readAsDataURL(file);
      });

      // input 초기화 (같은 파일 재선택 가능)
      e.target.value = "";
    },
    []
  );

  // 이미지 제거
  const removeImage = useCallback((index: number) => {
    setImages((prev) => prev.filter((_, i) => i !== index));
  }, []);

  return (
    <div className="input-container">
      {/* 이미지 미리보기 */}
      {images.length > 0 && (
        <div className="image-preview-bar">
          {images.map((img, idx) => (
            <div key={idx} className="image-preview-item">
              <img src={img.dataUrl} alt={`첨부 ${idx + 1}`} />
              <button
                className="image-remove-btn"
                onClick={() => removeImage(idx)}
                aria-label="이미지 제거"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="input-wrapper">
        <button
          className="attach-btn"
          onClick={() => fileInputRef.current?.click()}
          disabled={disabled}
          aria-label="이미지 첨부"
          title="이미지 첨부"
        >
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48" />
          </svg>
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          multiple
          hidden
          onChange={handleFileSelect}
        />
        <textarea
          ref={textareaRef}
          className="message-input"
          placeholder="메시지를 입력하세요..."
          rows={1}
          autoFocus
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
        />
        <button
          className="send-btn"
          disabled={disabled}
          onClick={doSend}
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
