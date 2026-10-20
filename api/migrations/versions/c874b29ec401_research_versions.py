"""Group immutable report revisions into research rounds.

Revision ID: c874b29ec401
Revises: 3f618bc985fc
"""

from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c874b29ec401"
down_revision = "3f618bc985fc"
branch_labels = None
depends_on = None


def _change_summary(content, parent):
    if parent is None:
        return {}
    before = {block["id"]: block for block in parent.get("blocks", [])}
    after = {block["id"]: block for block in content.get("blocks", [])}
    common = set(before) & set(after)
    return {"title": int(content.get("title") != parent.get("title")),
            "added": len(set(after) - set(before)), "removed": len(set(before) - set(after)),
            "modified": sum(before[key] != after[key] for key in common),
            "order": int([key for key in before if key in common] != [key for key in after if key in common]),
            "layout": int(content.get("layout") != parent.get("layout"))}


def upgrade():
    op.create_table("research_versions",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("report_id", sa.Uuid(as_uuid=False), sa.ForeignKey("reports.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("main_conversation_id", sa.Uuid(as_uuid=False), sa.ForeignKey("conversations.id"), unique=True),
        sa.Column("base_revision_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("initial_revision_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("current_revision_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("scope_version_id", sa.Uuid(as_uuid=False), sa.ForeignKey("scope_versions.id")),
        sa.Column("dataset_version_ids", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("analysis_id", sa.Uuid(as_uuid=False), sa.ForeignKey("analysis_results.id")),
        sa.UniqueConstraint("report_id", "number"))
    op.create_index("ix_research_versions_report_id", "research_versions", ["report_id"])
    for name in ("base_revision", "initial_revision", "current_revision"):
        op.create_foreign_key(f"fk_research_{name}", "research_versions", "report_versions", [f"{name}_id"], ["id"])
    op.add_column("reports", sa.Column("current_research_id", sa.Uuid(as_uuid=False)))
    op.add_column("reports", sa.Column("research_counter", sa.Integer(), server_default="0", nullable=False))
    op.create_foreign_key("fk_report_current_research", "reports", "research_versions", ["current_research_id"], ["id"])
    op.add_column("report_versions", sa.Column("research_id", sa.Uuid(as_uuid=False)))
    op.add_column("report_versions", sa.Column("change_kind", sa.String(20), server_default="edit", nullable=False))
    op.add_column("report_versions", sa.Column("restored_from_id", sa.Uuid(as_uuid=False)))
    op.add_column("report_versions", sa.Column("source_message_id", sa.Uuid(as_uuid=False)))
    op.add_column("report_versions", sa.Column("operation_id", sa.Uuid(as_uuid=False)))
    op.add_column("report_versions", sa.Column("change_summary", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column("report_versions", sa.Column("change_count", sa.Integer(), server_default="0", nullable=False))
    op.create_index("ix_report_versions_research_id", "report_versions", ["research_id"])
    op.create_foreign_key("fk_report_version_research", "report_versions", "research_versions", ["research_id"], ["id"])
    op.create_foreign_key("fk_report_version_restored_from", "report_versions", "report_versions", ["restored_from_id"], ["id"])
    op.create_foreign_key("fk_report_version_source_message", "report_versions", "messages", ["source_message_id"], ["id"])
    op.create_unique_constraint("uq_report_version_operation", "report_versions", ["operation_id"])

    connection = op.get_bind()
    metadata = sa.MetaData()
    researches = sa.Table("research_versions", metadata, autoload_with=connection)
    reports = sa.Table("reports", metadata, autoload_with=connection)
    versions_table = sa.Table("report_versions", metadata, autoload_with=connection)
    projects = {str(row["id"]): row for row in connection.execute(sa.text("SELECT * FROM projects")).mappings()}
    runs = {str(row["id"]): row for row in connection.execute(sa.text("SELECT id,payload FROM research_runs")).mappings()}
    messages = {str(row["id"]): row for row in connection.execute(sa.text(
        "SELECT id,run_id,role,conversation_id FROM messages ORDER BY created_at,id")).mappings()}
    user_by_run = {}
    for message in messages.values():
        if message["role"] == "user" and message["run_id"]:
            user_by_run.setdefault(str(message["run_id"]), str(message["id"]))
    for report in connection.execute(sa.select(reports)).mappings():
        versions = list(connection.execute(sa.select(versions_table).where(
            versions_table.c.report_id == report["id"]).order_by(versions_table.c.number)).mappings())
        by_id = {str(row["id"]): row for row in versions}
        head = by_id.get(str(report["current_version_id"]))
        project = projects[str(report["project_id"])]
        conversation_id = None
        cursor, seen = head, set()
        while cursor and str(cursor["id"]) not in seen:
            seen.add(str(cursor["id"]))
            if cursor["conversation_id"]:
                conversation_id = cursor["conversation_id"]
                break
            cursor = by_id.get(str(cursor["parent_id"]))
        research_id = str(uuid4())
        connection.execute(researches.insert().values(id=research_id, report_id=report["id"],
            created_at=versions[0]["created_at"] if versions else report["created_at"],
            status="published" if versions else "draft", number=1 if versions else None, revision=0,
            main_conversation_id=conversation_id,
            initial_revision_id=versions[0]["id"] if versions else None,
            current_revision_id=head["id"] if head else None,
            scope_version_id=head["scope_version_id"] if head else project["scope_version_id"],
            dataset_version_ids=head["dataset_version_ids"] if head else (
                [str(project["dataset_version_id"])] if project["dataset_version_id"] else []),
            analysis_id=head["analysis_id"] if head else project["current_analysis_id"]))
        connection.execute(reports.update().where(reports.c.id == report["id"]).values(
            current_research_id=research_id, research_counter=1 if versions else 0))
        for version in versions:
            parent = by_id.get(str(version["parent_id"]))
            summary = _change_summary(version["content"], parent["content"] if parent else None)
            run = runs.get(str(version["run_id"]))
            payload = run["payload"] or {} if run else {}
            message_id = payload.get("source_message_id") or payload.get("message_id") or user_by_run.get(str(version["run_id"]))
            message_id = message_id if message_id and str(message_id) in messages else None
            connection.execute(versions_table.update().where(versions_table.c.id == version["id"]).values(
                research_id=research_id, change_kind="edit" if parent else "generate",
                operation_id=version["run_id"], source_message_id=message_id,
                change_summary=summary, change_count=sum(summary.values())))


def downgrade():
    op.drop_constraint("uq_report_version_operation", "report_versions", type_="unique")
    for name in ("fk_report_version_source_message", "fk_report_version_restored_from", "fk_report_version_research"):
        op.drop_constraint(name, "report_versions", type_="foreignkey")
    op.drop_index("ix_report_versions_research_id", table_name="report_versions")
    for name in ("change_count", "change_summary", "operation_id", "source_message_id", "restored_from_id", "change_kind", "research_id"):
        op.drop_column("report_versions", name)
    op.drop_constraint("fk_report_current_research", "reports", type_="foreignkey")
    op.drop_column("reports", "research_counter")
    op.drop_column("reports", "current_research_id")
    op.drop_table("research_versions")
