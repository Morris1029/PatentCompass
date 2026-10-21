import pytest
from fastapi.testclient import TestClient

from app.errors import AppError
from app.main import check_reference, create_app
from app.markdown_text import markdown_visible_text
from app.models import Message, Project, Report, ReportVersion
from test_reports import report_analysis


@pytest.mark.parametrize(("source", "visible"), [
    ("这是**重点**和[链接](https://example.com) `a*b`。", "这是重点和链接 a*b。"),
    ("# **标题** 🚀\n\n正文中文😀", "标题 🚀 正文中文😀"),
    ("1. **第一项**\n2. [第二项](https://example.com)", "第一项 第二项"),
    ("- [x] **已完成**\n- [ ] 未完成", "已完成 未完成"),
    ("| **项目** | 数值 |\n| --- | --- |\n| [中文](https://example.com) | `1*2` |", "项目 数值 中文 1*2"),
    ("~~旧内容~~与~删除线~", "旧内容与删除线"),
    ("```html\n<b>**原样**</b>\n```", "<b>**原样**</b>"),
    ("前文 <b>保留文本</b> 后文", "前文 保留文本 后文"),
    ("**前**\n\n<div>隐藏HTML正文</div>\n\n**后**", "前 后"),
    ("&lt;b&gt;可见&lt;/b&gt; &amp; &#x1F680;", "<b>可见</b> & 🚀"),
    ("前![图片说明](https://example.com/a.png)后", "前后"),
    ("第一行  \n第二行\n\n> 引用内容", "第一行 第二行 引用内容"),
])
def test_markdown_visible_text_matches_selectable_gfm_text(source, visible):
    assert markdown_visible_text(source) == visible


@pytest.fixture
def quoted_report(db_session, project, context_factory, report_analysis):
    report = Report(project_id=project.id, version_counter=1)
    db_session.add(report)
    db_session.flush()
    run = context_factory(project, "generate_report")
    version = ReportVersion(report_id=report.id, number=1, run_id=run.run_id,
        analysis_id=report_analysis.id, scope_version_id=project.scope_version_id,
        dataset_version_ids=[project.dataset_version_id], content={"title": "引用测试", "blocks": [
            {"id": "heading", "type": "heading", "text": "# **研究结论** 🚀"},
            {"id": "paragraph", "type": "paragraph",
             "text": "这是**重点**和[证据链接](https://example.com)与`a*b`，中文😀。"},
            {"id": "list", "type": "paragraph", "text": "- **第一项**\n- [x] 第二项"},
            {"id": "markdown-table", "type": "paragraph",
             "text": "| **主题** | 数量 |\n| --- | --- |\n| 中文 | `1*2` |"},
            {"id": "html", "type": "paragraph", "text": "**前**\n\n<div>隐藏正文</div>\n\n**后**"},
            {"id": "structured-table", "type": "table", "rows": [["结构化表格内容"]]},
        ]})
    db_session.add(version)
    db_session.flush()
    report.current_version_id = version.id
    db_session.flush()
    return version


@pytest.fixture
def client(db_session):
    with TestClient(create_app(start_dispatcher=False)) as value:
        yield value


@pytest.mark.parametrize(("block_id", "quote"), [
    ("paragraph", "**重点**和[证据链接](https://example.com)"),
    ("paragraph", "重点和证据链接与a*b，中文😀"),
    ("heading", "研究结论 🚀"),
    ("list", "第一项\n第二项"),
    ("markdown-table", "主题\t数量\n中文\t1*2"),
    ("html", "前 后"),
])
def test_raw_and_visible_quotes_are_accepted_and_stored_unchanged(client, db_session, project, quoted_report,
                                                                block_id, quote):
    reference = {"report_version_id": quoted_report.id, "block_id": block_id, "text": quote}
    response = client.post(f"/api/projects/{project.id}/conversations", json={
        "initial_message": {"content": "解释这段文字", "action": "chat", "reference": reference}})
    assert response.status_code == 202, response.text
    db_session.expire_all()
    assert db_session.get(Message, response.json()["message_id"]).reference == {**reference, "block_ids": []}


@pytest.mark.parametrize(("block_id", "quote"), [
    ("paragraph", "这是伪造的重点和证据链接与a*b，中文😀"),
    ("paragraph", "证据链接 https://example.com"),
    ("html", "前 隐藏正文 后"),
    ("structured-table", "结构化表格内容"),
])
def test_visible_fallback_does_not_accept_fabricated_or_non_text_content(client, project, quoted_report,
                                                                      block_id, quote):
    response = client.post(f"/api/projects/{project.id}/conversations", json={"initial_message": {
        "content": "解释引用", "reference": {"report_version_id": quoted_report.id,
                                             "block_id": block_id, "text": quote}}})
    assert response.status_code == 409
    assert response.json()["code"] == "REFERENCE_TEXT_MISMATCH"


@pytest.mark.parametrize("quote", [
    "# **研究结论** 🚀 这是**重点**和[证据链接](https://example.com)",
    "研究结论 🚀\n这是重点和证据链接与a*b，中文😀。\n第一项 第二项",
])
def test_multiple_blocks_keep_raw_compatibility_and_report_order(db_session, project, quoted_report, quote):
    # The report's order remains authoritative even when client IDs arrive out of order.
    check_reference(db_session, project, {"report_version_id": quoted_report.id,
        "block_ids": ["list", "paragraph", "heading"], "text": quote})


def test_multiple_blocks_reject_reordered_quote_and_missing_block(db_session, project, quoted_report):
    reference = {"report_version_id": quoted_report.id, "block_ids": ["heading", "paragraph"],
                 "text": "这是重点和证据链接与a*b，中文😀。 研究结论 🚀"}
    with pytest.raises(AppError) as mismatch:
        check_reference(db_session, project, reference)
    assert mismatch.value.code == "REFERENCE_TEXT_MISMATCH"
    reference["block_ids"] = ["heading", "not-in-this-snapshot"]
    with pytest.raises(AppError) as missing:
        check_reference(db_session, project, reference)
    assert missing.value.status_code == 404
    assert missing.value.code == "BLOCK_NOT_FOUND"


def test_missing_block_in_real_snapshot_is_rejected(db_session, project, quoted_report):
    with pytest.raises(AppError) as failure:
        check_reference(db_session, project, {"report_version_id": quoted_report.id,
            "block_id": "not-in-this-snapshot", "text": "重点"})
    assert failure.value.status_code == 404
    assert failure.value.code == "BLOCK_NOT_FOUND"


def test_visible_quote_cannot_cross_project_or_owner(client, db_session, project, quoted_report):
    other = Project(owner_id=project.owner_id, name="另一项目")
    db_session.add(other)
    db_session.flush()
    reference = {"report_version_id": quoted_report.id, "block_id": "paragraph", "text": "重点和证据链接"}
    response = client.post(f"/api/projects/{other.id}/conversations", json={
        "initial_message": {"content": "解释引用", "reference": reference}})
    assert response.status_code == 404
    assert response.json()["code"] == "REFERENCE_NOT_FOUND"
    project.owner_id = "another-owner"
    db_session.flush()
    response = client.post(f"/api/projects/{other.id}/conversations", json={
        "initial_message": {"content": "解释引用", "reference": reference}})
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
