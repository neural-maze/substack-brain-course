"""week 3: failures table

Stores execution failures from inngest/function.failed system events.
Accumulates real-world failure cases for week 5 automated evaluation.

Revision ID: e4a7b9c1d2e3
Revises: c1f8a42e5b90
Create Date: 2026-10-14 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e4a7b9c1d2e3'
down_revision: Union[str, Sequence[str], None] = 'c1f8a42e5b90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'failures',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('function_id', sa.String(), nullable=False),
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('error', sa.Text(), nullable=False),
        sa.Column('event_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.drop_index('uq_passages_article_chunk', table_name='passages')
    op.create_index(
        'uq_passages_article_chunk',
        'passages',
        ['article_id', 'chunk_index'],
        unique=True,
        postgresql_where=sa.text('valid_to IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_passages_article_chunk', table_name='passages')
    op.create_index(
        'uq_passages_article_chunk',
        'passages',
        ['article_id', 'chunk_index'],
        unique=True,
    )
    op.drop_table('failures')
