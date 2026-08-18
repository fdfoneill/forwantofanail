"""Replace the seasonal forage flag with a bounded depletion counter."""

from alembic import op
import sqlalchemy as sa


revision = "20260818_0002"
down_revision = "20260818_0001"
branch_labels = None
depends_on = None


CONSTRAINT_NAME = "ck_locations_forage_depletion_range"


def _column_type() -> sa.types.TypeEngine:
    columns = sa.inspect(op.get_bind()).get_columns("locations")
    return next(column["type"] for column in columns if column["name"] == "foraged_this_season")


def _constraint_exists() -> bool:
    constraints = sa.inspect(op.get_bind()).get_check_constraints("locations")
    return any(constraint.get("name") == CONSTRAINT_NAME for constraint in constraints)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if isinstance(_column_type(), sa.Boolean):
        if dialect == "postgresql":
            op.execute("ALTER TABLE locations ALTER COLUMN foraged_this_season DROP DEFAULT")
            op.execute(
                "ALTER TABLE locations ALTER COLUMN foraged_this_season TYPE INTEGER "
                "USING CASE WHEN foraged_this_season THEN 1 ELSE 0 END"
            )
            op.execute("ALTER TABLE locations ALTER COLUMN foraged_this_season SET DEFAULT 0")
        else:
            with op.batch_alter_table("locations") as batch_op:
                batch_op.alter_column(
                    "foraged_this_season",
                    existing_type=sa.Boolean(),
                    type_=sa.Integer(),
                    existing_nullable=False,
                    server_default=sa.text("0"),
                )

    if not _constraint_exists():
        if dialect == "sqlite":
            with op.batch_alter_table("locations") as batch_op:
                batch_op.create_check_constraint(
                    CONSTRAINT_NAME,
                    "foraged_this_season >= 0 AND foraged_this_season <= 3",
                )
        else:
            op.create_check_constraint(
                CONSTRAINT_NAME,
                "locations",
                "foraged_this_season >= 0 AND foraged_this_season <= 3",
            )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if _constraint_exists():
        if dialect == "sqlite":
            with op.batch_alter_table("locations") as batch_op:
                batch_op.drop_constraint(CONSTRAINT_NAME, type_="check")
        else:
            op.drop_constraint(CONSTRAINT_NAME, "locations", type_="check")

    if isinstance(_column_type(), sa.Integer):
        if dialect == "postgresql":
            op.execute("ALTER TABLE locations ALTER COLUMN foraged_this_season DROP DEFAULT")
            op.execute(
                "ALTER TABLE locations ALTER COLUMN foraged_this_season TYPE BOOLEAN "
                "USING (foraged_this_season > 0)"
            )
            op.execute("ALTER TABLE locations ALTER COLUMN foraged_this_season SET DEFAULT FALSE")
        else:
            with op.batch_alter_table("locations") as batch_op:
                batch_op.alter_column(
                    "foraged_this_season",
                    existing_type=sa.Integer(),
                    type_=sa.Boolean(),
                    existing_nullable=False,
                    server_default=sa.text("0"),
                )
