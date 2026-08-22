"""sortirovka tablicy

Revision ID: d450a10753d3
Revises: dbd72c07311f
Create Date: 2026-08-20 23:14:40.329090

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd450a10753d3'
down_revision: Union[str, Sequence[str], None] = 'dbd72c07311f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('user_settings', schema=None) as batch_op:
        # Обе колонки nullable: пустое значение — это «пользователь порядок
        # не выбирал», и умолчание держит форма, а не база. server_default
        # тут поэтому не нужен.
        batch_op.add_column(sa.Column('sort_column', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('sort_dir', sa.String(length=4), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user_settings', schema=None) as batch_op:
        batch_op.drop_column('sort_dir')
        batch_op.drop_column('sort_column')
