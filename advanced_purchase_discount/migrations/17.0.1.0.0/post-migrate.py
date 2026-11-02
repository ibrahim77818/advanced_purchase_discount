# -*- coding: utf-8 -*-
"""16.0 -> 17.0

Odoo 17 ships a native ``discount`` field on vendor pricelists
(``product.supplierinfo``) and copies it on purchase order lines. The 16.0
version of this module stored the same information in ``apd_discount``:
copy it into the native field before the obsolete column is dropped.
"""
import logging

_logger = logging.getLogger(__name__)


def _column_exists(cr, table, column):
    cr.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version or not _column_exists(cr, 'product_supplierinfo', 'apd_discount'):
        return
    cr.execute("""
        UPDATE product_supplierinfo
           SET discount = apd_discount
         WHERE COALESCE(apd_discount, 0) <> 0
           AND COALESCE(discount, 0) = 0
    """)
    _logger.info("advanced_purchase_discount: %s vendor pricelist discount(s) moved to the native field",
                 cr.rowcount)
