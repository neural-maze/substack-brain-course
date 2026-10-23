"""week 5: eval_results and eval_runs tables

Stores online answer evaluation outcomes (`eval_results`, written by
`evaluate-answer` on `kb/answer.produced`) and offline/variant experiment runs
(`eval_runs`, written by `run-eval-suite` on `kb/eval.run`).

Revision ID: a7c9d1e2f3b4
Revises: f5b8c3d4e6f7
Create Date: 2026-10-23 17:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a7c9d1e2f3b4'
down_revision: Union[str, Sequence[str], None] = 'f5b8c3d4e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'eval_results',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('snapshot_id', sa.String(), nullable=False),
        sa.Column('deterministic_passed', sa.Boolean(), nullable=False),
        sa.Column('checks', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('faithfulness_verdict', sa.String(), nullable=False),
        sa.Column('faithfulness_critique', sa.Text(), nullable=False),
        sa.Column('disagreement_verdict', sa.String(), nullable=False),
        sa.Column('disagreement_critique', sa.Text(), nullable=False),
        sa.Column('failure_codes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('composite_score', sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['snapshot_id'], ['snapshots.snapshot_id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('snapshot_id', name='uq_eval_results_snapshot_id'),
    )
    op.create_table(
        'eval_runs',
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('suite_name', sa.String(), nullable=False),
        sa.Column('variants_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('case_results', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('run_id'),
    )


def downgrade() -> None:
    op.drop_table('eval_runs')
    op.drop_table('eval_results')

