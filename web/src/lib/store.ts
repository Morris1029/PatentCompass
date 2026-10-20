"use client";
import { create } from "zustand";
import { persist } from "zustand/middleware";
export type DraftAttachment = { localId: string; name: string; materialId?: string; runId?: string; status: string; error?: string };
type Draft = { content: string; reference?: { report_version_id: string; block_id?: string; block_ids?: string[]; text: string }; attachments?: DraftAttachment[]; submissionKey?: string };
type State = {
  collapsed: boolean; drafts: Record<string, Draft>; runs: Record<string, string[]>; cursors: Record<string, number>;
  collapse: () => void; draft: (key: string, patch: Partial<Draft>) => void; track: (project: string, run: string) => void; cursor: (run: string, seq: number) => void;
};
export const useStore = create<State>()(persist((set) => ({
  collapsed: false, drafts: {}, runs: {}, cursors: {},
  collapse: () => set(s => ({ collapsed: !s.collapsed })),
  draft: (key, patch) => set(s => {
    const previous = s.drafts[key];
    const changed = (patch.content !== undefined && patch.content !== previous?.content) || ("reference" in patch && JSON.stringify(patch.reference) !== JSON.stringify(previous?.reference));
    return { drafts: { ...s.drafts, [key]: { ...previous, content: previous?.content || "", ...patch, ...(changed && !("submissionKey" in patch) ? { submissionKey: undefined } : {}) } } };
  }),
  track: (project, run) => set(s => ({ runs: { ...s.runs, [project]: [...new Set([run, ...(s.runs[project] || [])])].slice(0, 30) } })),
  cursor: (run, seq) => set(s => ({ cursors: { ...s.cursors, [run]: Math.max(seq, s.cursors[run] || 0) } })),
}), { name: "patentcompass-ui-v1", skipHydration: true }));
