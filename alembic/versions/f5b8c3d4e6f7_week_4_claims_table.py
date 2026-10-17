"""week 4: claims table

Stores attributed, quote-verified claims extracted from articles with temporal
validity (`valid_from`, `valid_to`, `supersedes_id`) mirroring passages and
Memgraph `:Claim` nodes.

Revision ID: f5b8c3d4e6f7
Revises: e4a7b9c1d2e3
Create Date: 2026-10-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f5b8c3d4e6f7'
down_revision: Union[str, Sequence[str], None] = 'e4a7b9c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'claims',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('article_id', sa.UUID(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('quote_span', sa.Text(), nullable=False),
        sa.Column('claim_type', sa.String(), nullable=False),
        sa.Column('entities', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('confidence', sa.Numeric(precision=4, scale=3), nullable=False, server_default='1.0'),
        sa.Column('valid_from', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
        sa.Column('supersedes_id', sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(['article_id'], ['articles.id']),
        sa.ForeignKeyConstraint(['supersedes_id'], ['claims.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_claims_article_active',
        'claims',
        ['article_id'],
        unique=False,
        postgresql_where=sa.text('valid_to IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('ix_claims_article_active', table_name='claims')
    op.drop_table('claims')

