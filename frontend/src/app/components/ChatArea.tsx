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
 *   예: "| a | b || c | d |" → "| a | b |\n| c | d |"
 */
function preprocessMarkdown(content: string): string {
  let result = content;

  // 헤딩 앞에 개행 보정
  result = result.replace(/([^\n])(#{1,6}\s)/g, "$1\n\n$2");

  // 테이블 행이 || 로 이어진 경우 줄바꿈 삽입
  // "| ... || ..." 패턴을 "| ...\n| ..." 로 변환
  result = result.replace(/\|\s*\|\s*(?=\d+\s*\||\w)/g, "|\n| ");

  // 테이블 구분선이 없는 경우 보정: 헤더 행 다음에 구분선 삽입
  // 이미 구분선이 있으면 건드리지 않음
  const lines = result.split("\n");
  const processed: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    processed.push(lines[i]);
    // 현재 줄이 테이블 행이고, 다음 줄도 테이블 행인데 구분선(---)이 아닌 경우
    if (
      i === 0 ||
      !lines[i].trim().startsWith("|") ||
      !lines[i].trim().endsWith("|")
    ) continue;

    const nextLine = lines[i + 1]?.trim() || "";
    const prevLine = lines[i - 1]?.trim() || "";

    // 이전 줄이 테이블이 아니고, 다음 줄이 테이블인데 구분선이 아닌 경우 → 구분선 삽입
    if (
      !prevLine.startsWith("|") &&
      nextLine.startsWith("|") &&
      !nextLine.includes("---")
    ) {
      // 컬럼 수에 맞게 구분선 생성
      const cols = lines[i].split("|").length - 2;
      if (cols > 0) {
        const separator = "|" + " --- |".repeat(cols);
        processed.push(separator);
      }
    }
  }

  return processed.join("\n");
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
              {preprocessMarkdown(msg.content)}
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
