"""one price per line, store, and day

Saving the same typed line again for the same store and day now updates the price instead of
adding a row (owner, 2026-11-12). Before the unique index can exist, this collapses spacing in
typed lines (the save path does the same) and removes older copies, keeping the most recently
saved row of each line, which has the latest reading. Public dataset rows (no household) are
untouched: they are versioned by snapshot.

The downgrade drops the index only; removed copies are not restored.

Revision ID: a0e80346553f
Revises: 971b34d15b0a
Create Date: 2026-11-12 22:32:51.234420

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a0e80346553f'
down_revision = '971b34d15b0a'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE price_observation
        SET raw_text = regexp_replace(btrim(raw_text), '\\s+', ' ', 'g')
        WHERE household_id IS NOT NULL
          AND raw_text IS DISTINCT FROM regexp_replace(btrim(raw_text), '\\s+', ' ', 'g')
        """
    )
    op.execute(
        """
        DELETE FROM price_observation
        WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY household_id, vendor_id, observed_on, raw_text
                    ORDER BY created_at DESC, id DESC
                ) AS newest
                FROM price_observation
                WHERE household_id IS NOT NULL AND raw_text IS NOT NULL
            ) ranked
            WHERE newest > 1
        )
        """
    )
    op.create_index('uq_price_observation_line', 'price_observation', ['household_id', 'vendor_id', 'observed_on', 'raw_text'], unique=True, postgresql_where=sa.text('household_id IS NOT NULL'))


def downgrade():
    op.drop_index('uq_price_observation_line', table_name='price_observation', postgresql_where=sa.text('household_id IS NOT NULL'))
