"""week 6: research_runs and digests tables

Stores bounded research agent runs (`research_runs`, written by
`run-research-agent` on `kb/research.requested`) and weekly synthesis digests
(`digests`, written by `generate-weekly-digest` on `kb/digest.requested` or cron).

Revision ID: b8d2e4f6a1c3
Revises: a7c9d1e2f3b4
Create Date: 2026-10-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b8d2e4f6a1c3'
down_revision: Union[str, Sequence[str], None] = 'a7c9d1e2f3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'research_runs',
        sa.Column('research_id', sa.String(), nullable=False),
        sa.Column('event_id', sa.String(), nullable=True),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('plan', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('hops', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('report_text', sa.Text(), nullable=True),
        sa.Column('citations', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('retrieved', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('eval_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('confidence', sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column('iterations_used', sa.Integer(), nullable=False),
        sa.Column('total_cost_usd', sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('research_id'),
    )
    op.create_table(
        'digests',
        sa.Column('digest_id', sa.UUID(), nullable=False),
        sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('window_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('summary_markdown', sa.Text(), nullable=False),
        sa.Column('citations', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('stats', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('digest_id'),
    )


def downgrade() -> None:
    op.drop_table('digests')
    op.drop_table('research_runs')

