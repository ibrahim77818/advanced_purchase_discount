# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ApdDiscountTier(models.Model):
    _name = 'apd.discount.tier'
    _description = 'Purchase Volume Discount Tier'
    _order = 'sequence, scope, min_threshold desc, id'

    name = fields.Char(string='Name', required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', string='Company', required=True, index=True,
        default=lambda self: self.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id', string='Company Currency')
    partner_ids = fields.Many2many(
        'res.partner', 'apd_discount_tier_partner_rel', 'tier_id', 'partner_id',
        string='Vendors', help='Leave empty to apply to every vendor.')
    scope = fields.Selection([
        ('line', 'Per Line'),
        ('order', 'Whole Document'),
    ], string='Scope', required=True, default='line',
        help='Per Line: the threshold is evaluated on each line and the discount is applied to that line.\n'
             'Whole Document: the threshold is evaluated on the document totals and the discount is '
             'applied globally.')
    product_ids = fields.Many2many(
        'product.product', 'apd_discount_tier_product_rel', 'tier_id', 'product_id',
        string='Products', help='Leave empty to apply to every product.')
    categ_ids = fields.Many2many(
        'product.category', 'apd_discount_tier_categ_rel', 'tier_id', 'categ_id',
        string='Product Categories', help='Child categories are included. Leave empty for all.')
    threshold_type = fields.Selection([
        ('qty', 'Quantity'),
        ('amount', 'Untaxed Amount'),
    ], string='Threshold Based On', required=True, default='qty')
    min_threshold = fields.Float(
        string='Minimum Threshold', required=True, default=0.0,
        help='Quantity in the product unit of measure, or untaxed amount in the company currency.')
    discount_percent = fields.Float(string='Discount (%)', digits='Discount', required=True)
    apply_as = fields.Selection([
        ('prorate', 'Prorate on Lines'),
        ('discount_line', 'Dedicated Discount Line'),
    ], string='Apply As', default='prorate', required=True,
        help='Only for whole-document tiers.')
    date_start = fields.Date(string='Valid From')
    date_end = fields.Date(string='Valid Until')

    @api.constrains('discount_percent', 'min_threshold', 'date_start', 'date_end')
    def _check_values(self):
        for tier in self:
            if tier.discount_percent <= 0 or tier.discount_percent > 100:
                raise ValidationError(_('The tier discount must be greater than 0 and at most 100.'))
            if tier.min_threshold < 0:
                raise ValidationError(_('The minimum threshold cannot be negative.'))
            if tier.date_start and tier.date_end and tier.date_start > tier.date_end:
                raise ValidationError(_('The tier start date must be before its end date.'))

    def _apd_filter_valid(self, partner, date):
        commercial = partner.commercial_partner_id
        return self.filtered(lambda t: (
            (not t.partner_ids or commercial in t.partner_ids.commercial_partner_id or partner in t.partner_ids)
            and (not t.date_start or t.date_start <= date)
            and (not t.date_end or t.date_end >= date)
        ))

    def _apd_match_product(self, product):
        self.ensure_one()
        if self.product_ids and product not in self.product_ids:
            return False
        if self.categ_ids:
            categ = product.categ_id
            parents = self.env['product.category']
            while categ:
                parents |= categ
                categ = categ.parent_id
            if not (parents & self.categ_ids):
                return False
        return True
