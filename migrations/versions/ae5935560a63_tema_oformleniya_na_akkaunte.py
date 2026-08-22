"""tema oformleniya na akkaunte

Revision ID: ae5935560a63
Revises: d450a10753d3
Create Date: 2026-08-21 22:23:11.922096

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ae5935560a63'
down_revision: Union[str, Sequence[str], None] = 'd450a10753d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('user_settings', schema=None) as batch_op:
        # nullable без server_default: пустое значение — «тему не выбирали»,
        # и умолчание держит CSS, а не база. Всем, кто сохранял настройки
        # раньше, тема достаётся пустой — то есть тёмная, как и была.
        batch_op.add_column(sa.Column('theme', sa.String(length=8), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user_settings', schema=None) as batch_op:
        batch_op.drop_column('theme')
