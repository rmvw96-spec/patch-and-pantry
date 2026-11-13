"""compare by measure and nuts in shell

Spec P1-11 (owner, 2026-11-13): each catalog item is compared by a measure (a cup, or a piece:
lemons each, garlic by the head) and says whether its cups count toward produce cups. Nuts keep
SR28's shell refuse, and a price records whether it was for nuts in the shell. Storage borrowed
from another food's FoodKeeper entry names that food. Existing rows get the defaults: compared
by the cup, counted toward produce cups, not in the shell.

Best-buy amounts are now in the item's measure, so their fields lose "cups" from their names
(``cups_per_week`` is ``per_week``, and so on). Saved best-buy runs are rewritten to the new
names, with ``measure`` "cup", so they still read; the downgrade renames them back.

Revision ID: 86b6a123cd60
Revises: a0e80346553f
Create Date: 2026-11-13 11:55:09.488135

"""
import json

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = '86b6a123cd60'
down_revision = 'a0e80346553f'
branch_labels = None
depends_on = None


ROW_FIELDS = {
    "cups_per_week": "per_week",
    "cups_bought": "amount_bought",
    "usable_cups": "usable_amount",
    "cost_per_cup_cents": "cost_per_amount_cents",
    "cost_per_usable_cup_cents": "cost_per_usable_cents",
}
LINE_FIELDS = {"cups_per_week": "per_week", "usable_cups": "usable_amount"}


def _renamed(entry, names, add_measure):
    out = {names.get(key, key): value for key, value in entry.items()}
    if add_measure:
        out.setdefault("measure", "cup")
    else:
        out.pop("measure", None)
    return out


def _rewrite_best_buy_runs(forward):
    """Rename the best-buy fields in saved runs' input and result (both JSON)."""
    rows = op.get_bind().execute(
        sa.text("SELECT id, input, result FROM grocery_analysis_run WHERE kind = 'best_buy'")
    ).all()
    flip = (lambda d: d) if forward else (lambda d: {v: k for k, v in d.items()})
    rows_map, lines_map, needs_map = flip(ROW_FIELDS), flip(LINE_FIELDS), flip(
        {"cups_per_week": "per_week"}
    )
    for run_id, run_input, result in rows:
        run_input = json.loads(run_input) if isinstance(run_input, str) else run_input
        result = json.loads(result) if isinstance(result, str) else result
        if run_input and "needs" in run_input:
            run_input["needs"] = [
                _renamed(need, needs_map, False) for need in run_input["needs"]
            ]
        if result:
            result["rows"] = [
                _renamed(row, rows_map, forward) for row in result.get("rows", [])
            ]
            for group in result.get("shopping_list", []):
                group["lines"] = [
                    _renamed(line, lines_map, forward) for line in group.get("lines", [])
                ]
        op.get_bind().execute(
            sa.text(
                "UPDATE grocery_analysis_run SET input = CAST(:input AS json), "
                "result = CAST(:result AS json) WHERE id = :id"
            ),
            {
                "id": run_id,
                "input": json.dumps(run_input),
                # A run without a result keeps SQL NULL, not a JSON null.
                "result": None if result is None else json.dumps(result),
            },
        )


def upgrade():
    op.add_column('catalog_item', sa.Column('storage_as', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True))
    op.add_column('catalog_item', sa.Column('compared_by', sqlmodel.sql.sqltypes.AutoString(length=10), server_default='cup', nullable=False))
    op.add_column('catalog_item', sa.Column('produce_cups', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('catalog_item', sa.Column('shell_refuse_pct', sa.Float(), nullable=True))
    op.add_column('price_observation', sa.Column('in_shell', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.create_check_constraint(
        'ck_catalog_item_compared_by',
        'catalog_item',
        "compared_by IN ('cup', 'each', 'head')",
    )
    op.create_check_constraint(
        'ck_catalog_item_shell_refuse_pct',
        'catalog_item',
        'shell_refuse_pct > 0 AND shell_refuse_pct < 100',
    )
    _rewrite_best_buy_runs(forward=True)


def downgrade():
    _rewrite_best_buy_runs(forward=False)
    op.drop_constraint('ck_catalog_item_shell_refuse_pct', 'catalog_item', type_='check')
    op.drop_constraint('ck_catalog_item_compared_by', 'catalog_item', type_='check')
    op.drop_column('price_observation', 'in_shell')
    op.drop_column('catalog_item', 'shell_refuse_pct')
    op.drop_column('catalog_item', 'produce_cups')
    op.drop_column('catalog_item', 'compared_by')
    op.drop_column('catalog_item', 'storage_as')
