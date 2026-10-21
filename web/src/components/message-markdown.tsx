"use client";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function MarkdownContent({ content, inline = false }: { content: string; inline?: boolean }) {
  return <Markdown remarkPlugins={[remarkGfm]} skipHtml
    allowedElements={inline ? ["strong", "em", "del", "code", "a", "br"] : undefined}
    unwrapDisallowed={inline} components={{
    a: ({ children, href }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
    table: ({ children }) => <div className="message-table-wrap"><table>{children}</table></div>,
  }}>{content}</Markdown>;
}

export default function MessageMarkdown({ content }: { content: string }) {
  return <div className="message-markdown"><MarkdownContent content={content} /></div>;
}
