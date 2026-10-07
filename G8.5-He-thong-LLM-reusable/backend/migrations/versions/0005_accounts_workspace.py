"""Password/Google identities and conversation organization; preserve existing data."""

import sqlalchemy as sa
from alembic import op

revision = "0005_accounts_workspace"
down_revision = "0004_g5_feedback"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("users_display_name_key", "users", type_="unique")
    for name, size in (
        ("username", 50),
        ("password_hash", 500),
        ("google_subject", 255),
        ("email", 320),
    ):
        op.add_column("users", sa.Column(name, sa.String(size), nullable=True))
    op.create_unique_constraint("uq_users_username", "users", ["username"])
    op.create_unique_constraint("uq_users_google_subject", "users", ["google_subject"])
    op.add_column(
        "users", sa.Column("is_guest", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    for name in ("is_pinned", "title_is_manual"):
        op.add_column(
            "conversations",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    op.create_table(
        "auth_limits",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "oauth_challenges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("nonce_hash", sa.String(64), nullable=False),
        sa.Column("cookie_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    # Never discard credentials or merge same-name users on a production downgrade.
    connection = op.get_bind()
    unsafe = connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM users WHERE username IS NOT NULL "
            "OR password_hash IS NOT NULL OR google_subject IS NOT NULL OR is_guest) "
            "OR EXISTS(SELECT display_name FROM users GROUP BY display_name HAVING count(*) > 1)"
        )
    )
    if unsafe:
        raise RuntimeError("Restore a pre-migration backup to downgrade account identities.")
    op.drop_table("oauth_challenges")
    op.drop_table("auth_limits")
    for name in ("is_pinned", "title_is_manual"):
        op.drop_column("conversations", name)
    op.drop_constraint("uq_users_username", "users", type_="unique")
    op.drop_constraint("uq_users_google_subject", "users", type_="unique")
    for name in ("username", "password_hash", "google_subject", "email", "is_guest"):
        op.drop_column("users", name)
    op.create_unique_constraint("users_display_name_key", "users", ["display_name"])
