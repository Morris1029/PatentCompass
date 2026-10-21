type SelectionNode = {
  nodeType: number;
  nodeName: string;
  textContent: string | null;
  childNodes: ArrayLike<SelectionNode>;
};

const BLOCK_ELEMENTS = new Set([
  "BLOCKQUOTE", "DIV", "FIGCAPTION", "FIGURE", "H1", "H2", "H3", "H4", "H5", "H6",
  "HR", "LI", "OL", "P", "PRE", "SECTION", "TABLE", "TBODY", "TD", "TH", "THEAD", "TR", "UL",
]);

export function reportSelectionText(node: SelectionNode): string {
  if (node.nodeType === 3) return node.textContent || "";
  if (node.nodeName === "BR") return "\n";
  const text = Array.from(node.childNodes).map(reportSelectionText).join("");
  return BLOCK_ELEMENTS.has(node.nodeName) ? `\n${text}\n` : text;
}
