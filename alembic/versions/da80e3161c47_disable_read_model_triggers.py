"""disable_read_model_triggers

Revision ID: da80e3161c47
Revises: 382b962d0923
Create Date: 2026-02-03 14:36:19.583746

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'da80e3161c47'
down_revision: Union[str, Sequence[str], None] = '382b962d0923'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.execute("DROP TRIGGER IF EXISTS rm_on_game_phase_ready_aiud ON game_phase_ready;")
    op.execute("DROP TRIGGER IF EXISTS rm_on_game_players_aiud ON game_players;")
    op.execute("DROP TRIGGER IF EXISTS rm_on_game_sessions_aiu ON game_sessions;")

def downgrade():
    op.execute("""
    CREATE TRIGGER rm_on_game_sessions_aiu
    AFTER INSERT OR UPDATE ON game_sessions
    FOR EACH ROW EXECUTE FUNCTION trg_rm_on_game_sessions();
    """)
    op.execute("""
    CREATE TRIGGER rm_on_game_players_aiud
    AFTER INSERT OR UPDATE OR DELETE ON game_players
    FOR EACH ROW EXECUTE FUNCTION trg_rm_on_game_players();
    """)
    op.execute("""
    CREATE TRIGGER rm_on_game_phase_ready_aiud
    AFTER INSERT OR UPDATE OR DELETE ON game_phase_ready
    FOR EACH ROW EXECUTE FUNCTION trg_rm_on_game_phase_ready();
    """)
