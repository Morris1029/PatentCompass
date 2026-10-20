import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "专利导航 · 研究工作台", description: "以真实数据和原文证据推进专利导航研究" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN" data-scroll-behavior="smooth"><body className="draft ai-home workspace-shell">{children}</body></html>;
}
