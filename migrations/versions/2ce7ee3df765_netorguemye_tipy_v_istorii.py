"""netorguemye tipy v istorii

Revision ID: 2ce7ee3df765
Revises: b773882d66e3
Create Date: 2026-08-19 01:11:37.171703

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2ce7ee3df765'
down_revision: Union[str, Sequence[str], None] = 'b773882d66e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('market_history_state', schema=None) as batch_op:
        # Уже записанные пары считаем торгуемыми: до этапа 11.3 мы про
        # нетоварные типы не знали, и все состояния писались после успеха.
        batch_op.add_column(
            sa.Column('tradable', sa.Boolean(), nullable=False, server_default=sa.true())
        )



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('market_history_state', schema=None) as batch_op:
        batch_op.drop_column('tradable')

