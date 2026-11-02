# -*- coding: utf-8 -*-
from odoo import api, fields, models

MODE_SELECTION = [
    ('prorate', 'A - Proportional Proration'),
    ('manual', 'B - Manual Weighted Allocation'),
    ('discount_line', 'C - Dedicated Discount Line'),
    ('tiered', 'D - Smart Tiered Volume'),
    ('reset', 'Reset Discounts'),
]


class ApdDiscountApplication(models.Model):
    _name = 'apd.discount.application'
    _description = 'Purchase Discount Audit Log'
    _order = 'id desc'
    _rec_name = 'name'

    name = fields.Char(string='Reference', required=True, readonly=True, default='/')
    date = fields.Datetime(string='Applied On', default=fields.Datetime.now, readonly=True)
    user_id = fields.Many2one('res.users', string='Applied By', default=lambda self: self.env.user, readonly=True)
    company_id = fields.Many2one('res.company', string='Company', required=True, readonly=True, index=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)
    purchase_order_id = fields.Many2one('purchase.order', string='Purchase Order', readonly=True,
                                        index=True, ondelete='cascade')
    move_id = fields.Many2one('account.move', string='Vendor Bill', readonly=True, index=True, ondelete='cascade')
    partner_id = fields.Many2one('res.partner', string='Vendor', readonly=True)
    mode = fields.Selection(MODE_SELECTION, string='Mode', readonly=True)
    discount_type = fields.Selection([('percent', 'Percentage'), ('fixed', 'Fixed Amount')],
                                     string='Discount Type', readonly=True)
    discount_value = fields.Float(string='Entered Value', readonly=True, digits='Discount')
    replace_existing = fields.Boolean(string='Replaced Previous Discounts', readonly=True)
    amount_before = fields.Monetary(string='Untaxed Before', readonly=True)
    amount_after = fields.Monetary(string='Untaxed After', readonly=True)
    amount_discount = fields.Monetary(string='Discount Applied', readonly=True,
                                      help='Variation of the untaxed amount caused by this operation.')
    residual = fields.Monetary(string='Rounding Residual', readonly=True,
                               help='Difference between the requested and the achieved discount. '
                                    'Zero means the allocation is exact.')
    note = fields.Text(string='Reason', readonly=True)
    line_ids = fields.One2many('apd.discount.application.line', 'application_id', string='Details', readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('apd.discount.application') or '/'
        return super().create(vals_list)

    def action_open_document(self):
        self.ensure_one()
        record = self.move_id or self.purchase_order_id
        return {
            'type': 'ir.actions.act_window',
            'res_model': record._name,
            'res_id': record.id,
            'view_mode': 'form',
        }


class ApdDiscountApplicationLine(models.Model):
    _name = 'apd.discount.application.line'
    _description = 'Purchase Discount Audit Detail'
    _order = 'application_id, id'

    application_id = fields.Many2one('apd.discount.application', required=True, ondelete='cascade', index=True)
    currency_id = fields.Many2one(related='application_id.currency_id')
    description = fields.Char(string='Line')
    product_id = fields.Many2one('product.product', string='Product')
    action = fields.Selection([
        ('line_discount', 'Line Discount'),
        ('discount_line', 'Discount Line Added'),
        ('removed', 'Discount Line Removed'),
        ('reset', 'Discount Reset'),
    ], string='Action')
    discount_before = fields.Float(string='Disc. Before (%)', digits='Purchase Discount')
    discount_after = fields.Float(string='Disc. After (%)', digits='Purchase Discount')
    subtotal_before = fields.Monetary(string='Subtotal Before')
    subtotal_after = fields.Monetary(string='Subtotal After')
