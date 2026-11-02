# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ProductSupplierinfo(models.Model):
    _inherit = 'product.supplierinfo'

    apd_discount = fields.Float(
        string='Agreement Discount (%)',
        digits='Discount',
        default=0.0,
        help='Contractual discount granted by this vendor for this price line. Used by the tiered '
             'volume discount (Mode D) and, when enabled, copied on purchase order lines.',
    )

    @api.constrains('apd_discount')
    def _check_apd_discount(self):
        for rec in self:
            if rec.apd_discount < 0 or rec.apd_discount > 100:
                raise ValidationError(_('The agreement discount must be between 0 and 100.'))
