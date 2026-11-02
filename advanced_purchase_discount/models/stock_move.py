# -*- coding: utf-8 -*-
from odoo import fields, models


class StockMove(models.Model):
    _inherit = 'stock.move'

    apd_po_discount = fields.Float(
        related='purchase_line_id.discount', string='PO Disc. (%)', digits='Purchase Discount',
        help='Discount of the originating purchase order line. The move unit price is already net '
             'of this discount, so the stock valuation reflects the discounted cost.')
