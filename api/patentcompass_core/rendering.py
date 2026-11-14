"""Word and chart rendering with explicit renderer configuration."""

from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from .contracts import CapabilityError
from .reports import DEFAULT_REPORT_LAYOUT


@dataclass(frozen=True)
class RendererConfig:
    node_binary: str | Path
    script_path: str | Path
    timeout_seconds: float = 60


def render_chart(option, output_path=None, *, renderer: RendererConfig):
    """Return PNG bytes and optionally write them to an explicit destination."""
    if not renderer.node_binary or not Path(renderer.script_path).is_file():
        raise CapabilityError(503, "CHART_RENDERER_UNAVAILABLE", "图表导出环境尚未就绪")
    with TemporaryDirectory(prefix="patentcompass-chart-") as temporary:
        source = Path(temporary) / "option.json"
        target = Path(output_path) if output_path is not None else Path(temporary) / "chart.png"
        source.write_text(json.dumps(option, ensure_ascii=False), encoding="utf-8")
        try:
            subprocess.run([str(renderer.node_binary), str(renderer.script_path), "--input", str(source), "--output", str(target)],
                           check=True, capture_output=True, timeout=renderer.timeout_seconds)
        except (subprocess.SubprocessError, OSError) as error:
            raise CapabilityError(502, "CHART_RENDER_FAILED", "图表渲染失败，请检查导出服务日志") from error
        if not target.is_file():
            raise CapabilityError(502, "CHART_RENDER_FAILED", "图表渲染未生成图片")
        return target.read_bytes()


def _font(run, size, family):
    run.font.name = family
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor(0, 0, 0)
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), family)


def build_docx(content, version_number, *, renderer=None, render_chart_callable=None,
               default_layout=None, check_cancelled=lambda: None):
    """Render solely from a report version's deep snapshot, including chart options."""
    defaults = DEFAULT_REPORT_LAYOUT if default_layout is None else default_layout
    layout = {**deepcopy(defaults), **content.get("layout", {})}
    margins = {**defaults["margins_inches"], **layout["margins_inches"]}
    family, body_size = layout["font_family"], layout["body_font_pt"]
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Inches(layout["page_width_inches"]), Inches(layout["page_height_inches"])
    for edge, inches in margins.items():
        setattr(section, f"{edge}_margin", Inches(inches))
    for style_name in ("Normal", "Title", "Heading 1", "Heading 2", "Caption"):
        style = document.styles[style_name]
        style.font.name = family
        style.font.color.rgb = RGBColor(0, 0, 0)
        style._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), family)
    for border in document.styles["Title"]._element.xpath("./w:pPr/w:pBdr"):
        border.getparent().remove(border)
    normal = document.styles["Normal"]
    normal.font.size = Pt(body_size)
    normal.paragraph_format.space_after = Pt(7)
    normal.paragraph_format.line_spacing = 1.25
    document.add_paragraph(content["title"], "Title")
    analysis_id = content.get("source", {}).get("analysis_id")
    document.add_paragraph(f"报告版本 {version_number}" + (f"    分析结果 {analysis_id}" if analysis_id else ""))
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.add_run("第 ")
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    footer.add_run(" 页")
    with TemporaryDirectory(prefix="patentcompass-export-") as temporary:
        for index, block in enumerate(content["blocks"]):
            check_cancelled()
            kind = block["type"]
            if kind == "heading":
                document.add_heading(block["text"], level=block.get("level", 1))
            elif kind == "paragraph":
                document.add_paragraph(block["text"])
            elif kind == "table":
                caption = document.add_paragraph(block.get("caption", ""), "Caption")
                caption.paragraph_format.keep_with_next = True
                table = document.add_table(rows=1, cols=len(block["columns"]))
                table.style = "Table Grid"
                header = table.rows[0]
                repeat = OxmlElement("w:tblHeader")
                header._tr.get_or_add_trPr().append(repeat)
                for cell, text in zip(header.cells, block["columns"]):
                    cell.text = str(text)
                    shade = OxmlElement("w:shd")
                    shade.set(qn("w:fill"), "E8EEF5")
                    cell._tc.get_or_add_tcPr().append(shade)
                for values in block["rows"]:
                    for cell, value in zip(table.add_row().cells, values):
                        cell.text = str(value)
                for row in table.rows:
                    for cell_index, cell in enumerate(row.cells):
                        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                        usable_width = layout["page_width_inches"] - margins["left"] - margins["right"]
                        fraction = (0.8 if cell_index == 0 else 0.2) if len(block["columns"]) == 2 else 1 / len(block["columns"])
                        cell.width = Inches(usable_width * fraction)
                        properties = cell._tc.get_or_add_tcPr()
                        cell_margins = OxmlElement("w:tcMar")
                        for edge in ("top", "left", "bottom", "right"):
                            margin = OxmlElement(f"w:{edge}")
                            margin.set(qn("w:w"), "90")
                            margin.set(qn("w:type"), "dxa")
                            cell_margins.append(margin)
                        properties.append(cell_margins)
                        borders = OxmlElement("w:tcBorders")
                        for edge in ("top", "left", "bottom", "right"):
                            border = OxmlElement(f"w:{edge}")
                            for attr, value in (("val", "single"), ("sz", "4"), ("color", "D9D9D9")):
                                border.set(qn(f"w:{attr}"), value)
                            borders.append(border)
                        properties.append(borders)
                        for paragraph in cell.paragraphs:
                            paragraph.paragraph_format.space_after = Pt(3)
                            paragraph.paragraph_format.space_before = Pt(3)
                            if cell_index:
                                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            elif kind == "chart":
                path = Path(temporary) / f"chart-{index}.png"
                if render_chart_callable is not None:
                    render_chart_callable(block["chart"]["option"], path)
                elif renderer is not None:
                    render_chart(block["chart"]["option"], path, renderer=renderer)
                else:
                    raise CapabilityError(503, "CHART_RENDERER_UNAVAILABLE", "图表导出需要显式提供渲染器")
                paragraph = document.add_paragraph()
                chart_layout = {"width_percent": 100, "alignment": "center", **block.get("layout", {})}
                paragraph.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                                       "right": WD_ALIGN_PARAGRAPH.RIGHT}[chart_layout["alignment"]]
                paragraph.paragraph_format.keep_with_next = True
                usable_width = layout["page_width_inches"] - margins["left"] - margins["right"]
                width = min(layout["chart_width_inches"], usable_width) * chart_layout["width_percent"] / 100
                paragraph.add_run().add_picture(str(path), width=Inches(width))
                document.add_paragraph(block.get("caption", ""), "Caption")
                document.add_paragraph("来源：当前报告固定分析样本；计数单位为申请记录。", "Caption")
    for paragraph in document.paragraphs:
        for run in paragraph.runs:
            _font(run, 20 if paragraph.style.name == "Title" else
                  14 if paragraph.style.name == "Heading 1" else
                  11 if paragraph.style.name == "Heading 2" else body_size, family)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        _font(run, body_size - 1, family)
    output = BytesIO()
    document.save(output)
    return output.getvalue()
