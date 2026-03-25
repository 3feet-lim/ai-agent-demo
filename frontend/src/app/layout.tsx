import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "P리전 장애/이슈 분석 에이전트",
  description: "Observability AI Agent powered by LangChain + LangGraph + AWS Bedrock",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
