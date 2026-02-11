"""rename consumed_events offset column

Revision ID: d4d2ad8319b7
Revises: bf963ac052e2
Create Date: 2026-02-11 16:54:32.138262

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4d2ad8319b7'
down_revision: Union[str, Sequence[str], None] = 'bf963ac052e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.alter_column("consumed_events", "offset", new_column_name="kafka_offset")

def downgrade():
    op.alter_column("consumed_events", "kafka_offset", new_column_name="offset")