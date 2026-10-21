import test from "node:test";
import assert from "node:assert/strict";
import { reportSelectionText } from "../src/lib/report-selection.ts";

const text = value => ({ nodeType: 3, nodeName: "#text", textContent: value, childNodes: [] });
const element = (name, ...children) => ({ nodeType: name === "#document-fragment" ? 11 : 1, nodeName: name, textContent: null, childNodes: children });
const visible = node => reportSelectionText(node).replace(/\s+/g, " ").trim();

test("clipped report selections separate paragraphs and nested list items", () => {
  const fragment = element("#document-fragment",
    element("P", text("研究结论")),
    element("OL", element("LI", text("起飞"), element("UL", element("LI", text("视觉感知")), element("LI", text("融合定位")))), element("LI", text("降落"))),
    element("P", text("结束")));
  assert.equal(visible(fragment), "研究结论 起飞 视觉感知 融合定位 降落 结束");
});

test("clipped table selections retain separation between cells and rows", () => {
  const fragment = element("#document-fragment", element("TABLE",
    element("THEAD", element("TR", element("TH", text("技术")), element("TH", text("结论")))),
    element("TBODY", element("TR", element("TD", text("感知")), element("TD", element("STRONG", text("有效")))))));
  assert.equal(visible(fragment), "技术 结论 感知 有效");
});

test("line breaks separate text while inline emphasis, links and code do not split words", () => {
  const fragment = element("#document-fragment", element("P", text("无人"), element("STRONG", text("机")),
    element("A", text("起")), element("CODE", text("飞")), element("BR"), text("2024")));
  assert.equal(visible(fragment), "无人机起飞 2024");
});

test("partially clipped text and code retain the selected source without adding inline spaces", () => {
  const fragment = element("#document-fragment", text("局部"), element("EM", text("结论")),
    element("PRE", element("CODE", text("value = 1\nnext = 2"))), text("结束"));
  assert.equal(reportSelectionText(fragment), "局部结论\nvalue = 1\nnext = 2\n结束");
});
