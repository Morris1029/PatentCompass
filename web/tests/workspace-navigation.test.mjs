import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import ts from "typescript";

const require = createRequire(import.meta.url);
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const navigationUrl = moduleUrl(`
  let query = "";
  export const destinations = [];
  export function configure(value) { query = value; destinations.length = 0; }
  export function useSearchParams() { return new URLSearchParams(query); }
  export function usePathname() { return "/"; }
  export function useRouter() { return { push(url) { destinations.push(url); } }; }
`);
const emptyComponent = moduleUrl("export default function Component() { return null; }");
const imports = {
  react: moduleUrl("export function useEffect() {} export function useRef(value) { return { current: value }; } export function useState(value) { return [value, () => {}]; }"),
  "next/navigation": navigationUrl,
  "@/lib/api": moduleUrl(`
    export function useApi(path) {
      const data = path?.includes("research-versions/") ? { id: "research", number: 1, context: {} }
        : path?.includes("conversations/") ? { research_id: "research" }
        : path === "/api/projects/project" ? { id: "project", current_research_id: "research" }
        : { items: [] };
      return { data };
    }
    export function useReportVersion() { return {}; }
    export function post() { throw new Error("Navigation must not start a task"); }
    export function refresh() {}
  `),
  "@/lib/store": moduleUrl("export function useStore(select) { return select({ collapsed: false, collapse() {} }); }"),
  "@/lib/research": pathToFileURL(require.resolve("../src/lib/research.ts")).href,
  "./shared": moduleUrl("export function Empty() {} export function ErrorNotice() {} export function Modal() {}"),
  "./chat": emptyComponent, "./workbench": emptyComponent, "./report": emptyComponent, "./workspace-icon": emptyComponent,
};
const { outputText } = ts.transpileModule(readFileSync(new URL("../src/components/workspace.tsx", import.meta.url), "utf8"), {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext },
});
const { default: Workspace } = await import(moduleUrl(outputText.replace(/from ["']([^"']+)["']/g, (_, name) => `from "${imports[name] || pathToFileURL(require.resolve(name)).href}"`)));
const { configure, destinations } = await import(navigationUrl);
const text = node => Array.isArray(node) ? node.map(text).join("") : node?.props ? text(node.props.children) : typeof node === "string" ? node : "";
function clickSection(query, label) {
  configure(query);
  const visit = node => {
    if (Array.isArray(node)) return node.flatMap(visit);
    if (!node?.props) return [];
    return [...(node.type === "button" && text(node) === label ? [node] : []), ...visit(node.props.children)];
  };
  const buttons = visit(Workspace());
  assert.equal(buttons.length, 1);
  buttons[0].props.onClick();
  assert.equal(destinations.length, 1);
  return new URL(destinations[0], "http://localhost").searchParams;
}

test("top navigation preserves an explicitly linked historical analysis within the workbench", () => {
  const query = clickSection("project=project&view=workbench&tab=overview&analysis_id=historical-analysis&patent=historical-patent", "态势分析");
  assert.equal(query.get("tab"), "analysis");
  assert.equal(query.get("analysis_id"), "historical-analysis");
  assert.equal(query.get("patent"), "historical-patent");
  assert.equal(query.get("research"), null);
});

test("entering the workbench from a report retains its research and clears report-only selections", () => {
  const query = clickSection("project=project&view=report&research=research&version=revision&diff=previous&anchor=paragraph", "研究概览");
  assert.equal(query.get("view"), "workbench");
  assert.equal(query.get("research"), "research");
  for (const key of ["version", "diff", "anchor"]) assert.equal(query.get(key), null);
});

test("the promoted report entry retains the original adopted-report navigation", () => {
  const query = clickSection("project=project&view=workbench&research=older-research&version=revision&diff=previous", "导航报告");
  assert.equal(query.get("view"), "report");
  for (const key of ["research", "version", "diff"]) assert.equal(query.get(key), null);
});
