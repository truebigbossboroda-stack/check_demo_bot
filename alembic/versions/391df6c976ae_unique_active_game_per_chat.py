from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '391df6c976ae'
down_revision = 'fdecaa288dca'
branch_labels = None
depends_on = None


def upgrade():
    # 1) drop old constraint
    op.drop_constraint("ck_game_sessions_phase", "game_sessions", type_="check")

    # 2) normalize existing rows BEFORE adding new constraint
    op.execute(sa.text("""
        UPDATE game_sessions
        SET current_phase = CASE current_phase
          WHEN 'LOBBY'   THEN 'lobby'
          WHEN 'INCOME'  THEN 'income'
          WHEN 'ORDERS'  THEN 'orders'
          WHEN 'RESOLVE' THEN 'resolve'
          WHEN 'SUMMARY' THEN 'finished'
          ELSE lower(current_phase)
        END
    """))

    # 3) set new default (lowercase)
    op.alter_column(
        "game_sessions",
        "current_phase",
        existing_type=sa.Text(),
        server_default=sa.text("'lobby'"),
        existing_nullable=False,
    )

    # 4) add new constraint (full set)
    op.create_check_constraint(
        "ck_game_sessions_phase",
        "game_sessions",
        "current_phase IN ('lobby','income','event','world_arena','negotiations','orders','resolve','finished')"
    )

    # 5) expires_at default (чтобы не падало на NOT NULL при ручных INSERT)
    op.alter_column(
        "game_sessions",
        "expires_at",
        existing_type=sa.TIMESTAMP(timezone=True),
        server_default=sa.text("now() + interval '30 days'"),
        existing_nullable=False,
    )

def downgrade():
    # (минимально корректный откат — по желанию)
    op.drop_constraint("ck_game_sessions_phase", "game_sessions", type_="check")
    op.alter_column("game_sessions", "current_phase", existing_type=sa.Text(),
                    server_default=sa.text("'LOBBY'"), existing_nullable=False)
    op.create_check_constraint(
        "ck_game_sessions_phase",
        "game_sessions",
        "current_phase IN ('LOBBY','INCOME','ORDERS','RESOLVE','SUMMARY')"
    )
    op.alter_column("game_sessions", "expires_at", existing_type=sa.TIMESTAMP(timezone=True),
                    server_default=None, existing_nullable=False)

    # данные обратно не “поднять” без потерь — это нормально

