"use client";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function MessageMarkdown({ content }: { content: string }) {
  return <div className="message-markdown"><Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
    a: ({ children, href }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
    table: ({ children }) => <div className="message-table-wrap"><table>{children}</table></div>,
  }}>{content}</Markdown></div>;
}
