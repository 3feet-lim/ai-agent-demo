"use client";

import { useEffect, useRef } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import rehypeRaw from "rehype-raw";
// 모든 언어가 포함된 Prism 번들 사용 (별도 등록 불필요)
import SyntaxHighlighter from "react-syntax-highlighter/dist/esm/prism-light";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import hcl from "react-syntax-highlighter/dist/esm/languages/prism/hcl";
import docker from "react-syntax-highlighter/dist/esm/languages/prism/docker";
import ini from "react-syntax-highlighter/dist/esm/languages/prism/ini";
import nginx from "react-syntax-highlighter/dist/esm/languages/prism/nginx";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";

SyntaxHighlighter.registerLanguage("bash", bash);
SyntaxHighlighter.registerLanguage("shell", bash);
SyntaxHighlighter.registerLanguage("sh", bash);
SyntaxHighlighter.registerLanguage("yaml", yaml);
SyntaxHighlighter.registerLanguage("yml", yaml);
SyntaxHighlighter.registerLanguage("json", json);
SyntaxHighlighter.registerLanguage("sql", sql);
SyntaxHighlighter.registerLanguage("hcl", hcl);
SyntaxHighlighter.registerLanguage("terraform", hcl);
SyntaxHighlighter.registerLanguage("docker", docker);
SyntaxHighlighter.registerLanguage("dockerfile", docker);
SyntaxHighlighter.registerLanguage("ini", ini);
SyntaxHighlighter.registerLanguage("nginx", nginx);
SyntaxHighlighter.registerLanguage("python", python);
SyntaxHighlighter.registerLanguage("javascript", javascript);
SyntaxHighlighter.registerLanguage("typescript", typescript);

interface Message {
  role: string;
  content: string;
  images?: string[];
  toolTrace?: string[];
  activeTools?: string[];
}

interface ChatAreaProps {
  messages: Message[];
  isLoading: boolean;
  isStreaming?: boolean;
}

// 화이트 톤에 맞춘 코드 블록 커스텀 스타일
const codeTheme = {
  ...oneLight,
  'pre[class*="language-"]': {
    ...oneLight['pre[class*="language-"]'],
    background: "#f5f6f8",
    borderRadius: "8px",
    padding: "16px",
    margin: "10px 0",
    fontSize: "13px",
    border: "1px solid #e0e3e8",
  },
  'code[class*="language-"]': {
    ...oneLight['code[class*="language-"]'],
    background: "transparent",
    fontSize: "13px",
  },
};

/**
 * 코드 내용을 기반으로 언어를 자동 감지
 * 패턴 매칭으로 bash, python, json, yaml, sql 등을 구분
 */
function detectLanguage(code: string): string {
  const trimmed = code.trim();

  // JSON: { 또는 [ 로 시작
  if (/^\s*[\[{]/.test(trimmed) && /[\]}]\s*$/.test(trimmed)) return "json";

  // YAML: key: value 패턴
  if (/^[\w-]+:\s/m.test(trimmed) && !trimmed.includes("{")) return "yaml";

  // SQL: SELECT, INSERT, CREATE, ALTER 등
  if (/^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|FROM|WHERE)\b/im.test(trimmed)) return "sql";

  // Python: def, import, class, print(
  if (/^\s*(def |import |from |class |print\(|if __name__)/m.test(trimmed)) return "python";

  // Dockerfile: FROM, RUN, COPY, CMD
  if (/^\s*(FROM |RUN |COPY |CMD |ENTRYPOINT |WORKDIR |EXPOSE )/m.test(trimmed)) return "docker";

  // Shell/Bash: aws, kubectl, docker, sudo, export, echo, #!
  if (/^\s*(#!\/bin\/(ba)?sh|aws |kubectl |docker |sudo |export |echo |curl |wget |chmod |mkdir |cd |ls |cat |grep |pip |npm |yarn |apt |yum )/m.test(trimmed)) return "bash";

  // Shell: 명령어 옵션 패턴 (--option)
  if (/^\s*\w[\w-]*\s+--[\w-]+/m.test(trimmed)) return "bash";

  // HCL/Terraform: resource, variable, module 블록
  if (/^\s*(resource|variable|module|provider|output|data)\s+"/.test(trimmed)) return "hcl";

  // 기본값: bash (인프라 도구 특성상 shell 명령이 많음)
  return "bash";
}

/**
 * 마크다운 전처리: 깨진 테이블/헤딩 보정
 * - 헤딩(##) 앞에 개행이 없으면 추가
 * - 테이블 행이 한 줄로 이어진 경우 줄바꿈 삽입
 * - 테이블 구분선이 없으면 자동 삽입
 * - 스트리밍 중 불완전한 테이블은 코드블록으로 임시 보호
 */
function preprocessMarkdown(content: string, isStreaming?: boolean): string {
  let result = content;

  // 헤딩 앞에 개행 보정
  result = result.replace(/([^\n])(#{1,6}\s)/g, "$1\n\n$2");

  const lines = result.split("\n");
  const processed: string[] = [];

  // 테이블 블록을 감지하여 처리
  let tableBlock: string[] = [];
  let inTable = false;

  // 구분선 판별: | 와 -, :, 공백만으로 구성된 행
  const isSeparatorRow = (line: string) => {
    const t = line.trim();
    // | --- | --- | 또는 |---|---| 또는 | :---: | 등
    return /^\|[-\s:|]+\|$/.test(t) && t.includes("-");
  };

  const flushTable = () => {
    if (tableBlock.length === 0) return;

    const hasSeparator = tableBlock.some((l) => isSeparatorRow(l));

    if (!hasSeparator && tableBlock.length >= 2) {
      // 구분선이 없으면 첫 번째 행 뒤에 자동 삽입
      const headerRow = tableBlock[0].trim();
      const cols = headerRow.split("|").filter((_, idx, arr) => idx > 0 && idx < arr.length - 1).length;
      if (cols > 0) {
        const separator = "|" + " --- |".repeat(cols);
        tableBlock.splice(1, 0, separator);
      }
    } else if (!hasSeparator && tableBlock.length === 1 && isStreaming) {
      // 스트리밍 중 헤더만 도착 → raw 텍스트 그대로 출력
      processed.push(...tableBlock);
      tableBlock = [];
      return;
    }

    processed.push(...tableBlock);
    tableBlock = [];
  };

  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].trim();
    const isTableRow = trimmed.startsWith("|") && trimmed.endsWith("|") && trimmed.length > 1;

    if (isTableRow) {
      if (!inTable) inTable = true;
      tableBlock.push(lines[i]);
    } else {
      if (inTable) {
        flushTable();
        inTable = false;
        // 스트리밍 중 불완전한 행 (| 로 시작하지만 | 로 안 끝남) → 버퍼에 안 넣고 그대로 출력
        if (isStreaming && trimmed.startsWith("|") && !trimmed.endsWith("|")) {
          processed.push(lines[i]);
        } else {
          processed.push(lines[i]);
        }
      } else {
        processed.push(lines[i]);
      }
    }
  }

  // 남은 테이블 블록 처리
  if (tableBlock.length > 0) {
    flushTable();
  }

  return processed.join("\n");
}

export default function ChatArea({ messages, isLoading, isStreaming }: ChatAreaProps) {
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
          <h2>안녕하세요, Olly입니다!</h2>
          <p>무엇을 도와드릴까요? 질문을 입력해 주세요.</p>
        </div>
      )}

      {messages.map((msg, idx) => (
        <div key={idx} className={`message ${msg.role}`}>
          <div className="message-avatar">
            {msg.role === "user" ? "U" : "Olly"}
          </div>
          <div className="message-content">
            {/* 첨부 이미지 표시 */}
            {msg.images && msg.images.length > 0 && (
              <div className="message-images">
                {msg.images.map((src, i) => (
                  <img
                    key={i}
                    src={src}
                    alt={`첨부 이미지 ${i + 1}`}
                    className="message-image"
                  />
                ))}
              </div>
            )}
            {/* 도구 실행 중 표시 */}
            {msg.role === "assistant" && msg.activeTools && msg.activeTools.length > 0 && (
              <div className="tool-activity">
                <span className="tool-activity-icon">⚙️</span>
                <span>{msg.activeTools[msg.activeTools.length - 1]} 실행 중...</span>
              </div>
            )}
            <Markdown
              remarkPlugins={[remarkGfm, remarkBreaks]}
              rehypePlugins={[rehypeRaw]}
              components={{
                // 테이블을 스크롤 가능한 wrapper로 감싸기
                table({ children, ...props }) {
                  return (
                    <div className="table-wrapper">
                      <table {...props}>{children}</table>
                    </div>
                  );
                },
                code({ className, children, ...props }) {
                  const match = /language-(\w+)/.exec(className || "");
                  const codeString = String(children).replace(/\n$/, "");
                  const isMultiLine = codeString.includes("\n");

                  // 언어가 명시된 경우
                  if (match) {
                    return (
                      <div className="code-block-wrapper">
                        <span className="code-lang-label">{match[1]}</span>
                        <SyntaxHighlighter
                          style={codeTheme}
                          language={match[1]}
                          PreTag="div"
                        >
                          {codeString}
                        </SyntaxHighlighter>
                      </div>
                    );
                  }

                  // 언어 미지정 멀티라인 코드 블록 → 자동 감지
                  if (isMultiLine) {
                    const lang = detectLanguage(codeString);
                    return (
                      <div className="code-block-wrapper">
                        <span className="code-lang-label">{lang}</span>
                        <SyntaxHighlighter
                          style={codeTheme}
                          language={lang}
                          PreTag="div"
                        >
                          {codeString}
                        </SyntaxHighlighter>
                      </div>
                    );
                  }

                  // 인라인 코드
                  return (
                    <code className={className} {...props}>
                      {children}
                    </code>
                  );
                },
              }}
            >
              {preprocessMarkdown(msg.content, isStreaming && idx === messages.length - 1)}
            </Markdown>
            {/* 도구 호출 이력 표시 */}
            {msg.role === "assistant" && msg.toolTrace && msg.toolTrace.length > 0 && (
              <div className="tool-trace">
                <details>
                  <summary>🔧 사용된 도구 ({msg.toolTrace.length}개)</summary>
                  <ol className="tool-trace-list">
                    {msg.toolTrace.map((tool, i) => (
                      <li key={i}>{tool}</li>
                    ))}
                  </ol>
                </details>
              </div>
            )}
          </div>
        </div>
      ))}

      {isLoading && (
        <div className="message assistant">
          <div className="message-avatar">Olly</div>
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
