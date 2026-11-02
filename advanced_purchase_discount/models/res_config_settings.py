# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    apd_discount_enabled = fields.Boolean(
        related='company_id.apd_discount_enabled', readonly=False)
    apd_discount_product_id = fields.Many2one(
        related='company_id.apd_discount_product_id', readonly=False)

    @api.model_create_multi
    def create(self, vals_list):
        # Related fields are written one by one on the company; defer the
        # "product required" check until all of them are written.
        return super(ResConfigSettings, self.with_context(apd_defer_company_check=True)).create(vals_list)

    def set_values(self):
        res = super().set_values()
        company = self.company_id
        if company.apd_discount_enabled and not company.apd_discount_product_id:
            raise ValidationError(_(
                'Select a discount service product to enable Advanced Purchase Discounts.'))
        return res
