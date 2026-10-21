"use client";
import { useEffect, type RefObject } from "react";

type ViewportSample = { height: number; offsetTop: number; scale: number };
export type ChatViewportGeometry = { height: number; offsetTop: number };

export function chatViewportGeometry(sample: ViewportSample, previous?: ChatViewportGeometry): ChatViewportGeometry | undefined {
  if (!Number.isFinite(sample.height) || sample.height <= 0 || !Number.isFinite(sample.offsetTop) || !Number.isFinite(sample.scale) || Math.abs(sample.scale - 1) > 0.01) return previous;
  const offsetTop = Math.max(0, sample.offsetTop);
  return previous?.height === sample.height && previous.offsetTop === offsetTop ? previous : { height: sample.height, offsetTop };
}

export function useChatViewport(ref: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const frame = ref.current?.closest<HTMLElement>(".workspace-view-chat");
    const viewport = window.visualViewport;
    if (!frame || !viewport) return;
    let geometry: ChatViewportGeometry | undefined;
    let scheduled: number | undefined;
    const update = () => {
      scheduled = undefined;
      const next = chatViewportGeometry(viewport, geometry);
      if (!next || next === geometry) return;
      geometry = next;
      frame.style.setProperty("--chat-viewport-height", `${next.height}px`);
      frame.style.setProperty("--chat-viewport-top", `${next.offsetTop}px`);
    };
    const schedule = () => { if (scheduled === undefined) scheduled = window.requestAnimationFrame(update); };
    update();
    viewport.addEventListener("resize", schedule);
    viewport.addEventListener("scroll", schedule);
    window.addEventListener("resize", schedule);
    return () => {
      viewport.removeEventListener("resize", schedule);
      viewport.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      if (scheduled !== undefined) window.cancelAnimationFrame(scheduled);
      frame.style.removeProperty("--chat-viewport-height");
      frame.style.removeProperty("--chat-viewport-top");
    };
  }, [ref]);
}
