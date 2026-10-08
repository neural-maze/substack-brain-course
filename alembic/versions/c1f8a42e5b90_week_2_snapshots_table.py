"""week 2: snapshots table

One row per answer from POST /ask, with every retrieved candidate (not just
the cited ones), so week 5's evaluators can tell an ingestion problem from a
retrieval problem from a generation problem.

Revision ID: c1f8a42e5b90
Revises: 91cd4b8a59f2
Create Date: 2026-10-01 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c1f8a42e5b90'
down_revision: Union[str, Sequence[str], None] = '91cd4b8a59f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'snapshots',
        sa.Column('snapshot_id', sa.String(), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('answer_text', sa.Text(), nullable=False),
        sa.Column('citations', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('retrieved', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('retriever_variant', sa.String(), nullable=False),
        sa.Column('as_of', sa.DateTime(timezone=True), nullable=True),
        sa.Column('confidence', sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('snapshot_id'),
    )
    op.create_index('ix_snapshots_created_at', 'snapshots', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_snapshots_created_at', table_name='snapshots')
    op.drop_table('snapshots')
