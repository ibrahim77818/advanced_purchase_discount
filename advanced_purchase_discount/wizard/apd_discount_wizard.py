# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError

MODES = [
    ('prorate', 'A - Proportional Proration'),
    ('manual', 'B - Manual Weighted Allocation'),
    ('discount_line', 'C - Dedicated Discount Line'),
    ('tiered', 'D - Smart Tiered Volume'),
]


class ApdDiscountWizard(models.TransientModel):
    _name = 'apd.discount.wizard'
    _description = 'Apply Purchase Discount'

    purchase_order_id = fields.Many2one('purchase.order', string='Purchase Order', readonly=True)
    move_id = fields.Many2one('account.move', string='Vendor Bill', readonly=True)
    company_id = fields.Many2one('res.company', compute='_compute_document', store=True)
    currency_id = fields.Many2one('res.currency', compute='_compute_document', store=True)
    discount_product_id = fields.Many2one(related='company_id.apd_discount_product_id', string='Discount Product')

    mode = fields.Selection(MODES, string='Distribution Mode', required=True, default='prorate')
    discount_type = fields.Selection([
        ('percent', 'Percentage'),
        ('fixed', 'Fixed Amount'),
    ], string='Discount Type', required=True, default='percent')
    discount_percent = fields.Float(string='Discount (%)', digits='Discount')
    discount_amount = fields.Monetary(string='Discount Amount',
                                      help='Untaxed (tax excluded) amount to deduct.')
    prorate_basis = fields.Selection([
        ('amount', 'Proportional to line amount'),
        ('quantity', 'Proportional to quantity'),
        ('equal', 'Evenly on each line'),
    ], string='Distribution Basis', default='amount', required=True,
        help='How a fixed amount is spread on the selected lines.')
    manual_basis = fields.Selection([
        ('weight', 'Total discount split by weights'),
        ('line_percent', 'Specific percentage per line'),
        ('line_amount', 'Specific amount per line'),
    ], string='Allocation', default='weight', required=True)
    replace_existing = fields.Boolean(
        string='Replace Existing Discounts', default=True,
        help='Reset every line discount and remove previous discount lines before applying. '
             'When unchecked, the new discount is stacked on top of the existing ones.')
    note = fields.Text(string='Reason')
    line_ids = fields.One2many('apd.discount.wizard.line', 'wizard_id', string='Lines')

    amount_before = fields.Monetary(string='Untaxed Before', compute='_compute_preview')
    amount_after = fields.Monetary(string='Untaxed After', compute='_compute_preview')
    amount_discount = fields.Monetary(string='Discount Variation', compute='_compute_preview')
    preview_residual = fields.Monetary(string='Rounding Residual', compute='_compute_preview')
    preview_message = fields.Text(string='Details', compute='_compute_preview')
    preview_error = fields.Char(string='Warning', compute='_compute_preview')

    @api.depends('purchase_order_id', 'move_id')
    def _compute_document(self):
        for wizard in self:
            doc = wizard._get_document()
            wizard.company_id = doc.company_id if doc else self.env.company
            wizard.currency_id = doc.currency_id if doc else self.env.company.currency_id

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'line_ids' not in fields_list:
            return res
        ctx = self.env.context
        doc = False
        po_id = res.get('purchase_order_id') or ctx.get('default_purchase_order_id')
        move_id = res.get('move_id') or ctx.get('default_move_id')
        if po_id:
            doc = self.env['purchase.order'].browse(po_id)
        elif move_id:
            doc = self.env['account.move'].browse(move_id)
        if not doc:
            raise UserError(_('The discount wizard must be opened from a purchase order or a vendor bill.'))
        doc._apd_check_editable()
        commands = []
        for seq, line in enumerate(doc._apd_eligible_lines(), start=1):
            gross = line._apd_untaxed(0.0)
            vals = {
                'sequence': seq,
                'name': line._apd_label()[:256],
                'product_id': line.product_id.id,
                'quantity': line._apd_quantity(),
                'price_unit': line.price_unit,
                'current_discount': line.discount,
                'gross_subtotal': gross,
                'include': True,
                'weight': 1.0,
            }
            vals['purchase_line_id' if doc._name == 'purchase.order' else 'move_line_id'] = line.id
            commands.append((0, 0, vals))
        res['line_ids'] = commands
        return res

    def _get_document(self):
        self.ensure_one()
        return self.purchase_order_id or self.move_id

    def _get_params(self, mode=None):
        self.ensure_one()
        lines = {}
        for wl in self.line_ids:
            source = wl.purchase_line_id or wl.move_line_id
            if source:
                lines[source.id] = {
                    'include': wl.include,
                    'weight': wl.weight,
                    'percent': wl.line_percent,
                    'amount': wl.line_amount,
                }
        return {
            'mode': mode or self.mode,
            'discount_type': self.discount_type,
            'value': self.discount_percent if self.discount_type == 'percent' else self.discount_amount,
            'prorate_basis': self.prorate_basis,
            'manual_basis': self.manual_basis,
            'replace': self.replace_existing,
            'lines': lines,
        }

    def _get_plan(self, mode=None):
        doc = self._get_document()
        engine = self.env['apd.discount.engine'].with_company(doc.company_id)
        return engine._build_plan(doc, self._get_params(mode))

    @api.depends('mode', 'discount_type', 'discount_percent', 'discount_amount', 'prorate_basis', 'manual_basis',
                 'replace_existing', 'line_ids.include', 'line_ids.weight', 'line_ids.line_percent',
                 'line_ids.line_amount')
    def _compute_preview(self):
        for wizard in self:
            wizard.preview_error = False
            wizard.preview_message = False
            wizard.amount_before = wizard.amount_after = wizard.amount_discount = 0.0
            wizard.preview_residual = 0.0
            doc = wizard._get_document()
            if not doc:
                continue
            try:
                plan = wizard._get_plan()
            except UserError as e:
                wizard.preview_error = e.args[0] if e.args else str(e)
                wizard.amount_before = wizard.amount_after = doc._apd_amount_untaxed()
                continue
            wizard.amount_before = plan['amount_before']
            wizard.amount_after = plan['amount_after']
            wizard.amount_discount = plan['amount_discount']
            wizard.preview_residual = plan['residual']
            messages = list(plan['notes'])
            for spec in plan['new_discount_lines']:
                messages.append(_('New discount line: %(name)s = %(amount)s',
                                  name=spec['name'], amount=-spec['amount']))
            if plan['remove_lines']:
                messages.append(_('%s previous discount line(s) will be removed.', len(plan['remove_lines'])))
            wizard.preview_message = '\n'.join(messages) or False

    def action_apply(self):
        self.ensure_one()
        doc = self._get_document()
        doc._apd_check_editable()
        params = self._get_params()
        engine = self.env['apd.discount.engine'].with_company(doc.company_id)
        plan = engine._build_plan(doc, params)
        if not plan['changed'] and not plan['remove_lines'] and not plan['new_discount_lines']:
            raise UserError(_('Nothing to apply: the discount does not change the document.'))
        engine._apply_plan(doc, plan, params, note=self.note)
        return {'type': 'ir.actions.act_window_close'}

    def action_reset(self):
        self.ensure_one()
        doc = self._get_document()
        doc._apd_check_editable()
        params = self._get_params(mode='reset')
        engine = self.env['apd.discount.engine'].with_company(doc.company_id)
        plan = engine._build_plan(doc, params)
        if not plan['changed'] and not plan['remove_lines']:
            raise UserError(_('There is no discount to reset on this document.'))
        engine._apply_plan(doc, plan, params, note=self.note)
        return {'type': 'ir.actions.act_window_close'}


class ApdDiscountWizardLine(models.TransientModel):
    _name = 'apd.discount.wizard.line'
    _description = 'Apply Purchase Discount - Line'
    _order = 'sequence, id'

    wizard_id = fields.Many2one('apd.discount.wizard', required=True, ondelete='cascade')
    currency_id = fields.Many2one(related='wizard_id.currency_id')
    sequence = fields.Integer()
    purchase_line_id = fields.Many2one('purchase.order.line', readonly=True)
    move_line_id = fields.Many2one('account.move.line', readonly=True)
    name = fields.Char(string='Description', readonly=True)
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    quantity = fields.Float(string='Quantity', digits='Product Unit', readonly=True)
    price_unit = fields.Float(string='Unit Price', digits='Product Price', readonly=True)
    current_discount = fields.Float(string='Current Disc. (%)', digits='Purchase Discount', readonly=True)
    gross_subtotal = fields.Monetary(string='Gross Subtotal', readonly=True)
    include = fields.Boolean(string='Apply', default=True)
    weight = fields.Float(string='Weight', default=1.0, digits=(16, 4))
    line_percent = fields.Float(string='Line Disc. (%)', digits='Discount')
    line_amount = fields.Monetary(string='Line Discount Amount')
    new_discount = fields.Float(string='New Disc. (%)', digits='Purchase Discount',
                                compute='_compute_new_values')
    new_subtotal = fields.Monetary(string='New Subtotal', compute='_compute_new_values')

    @api.depends('wizard_id.mode', 'wizard_id.discount_type', 'wizard_id.discount_percent', 'wizard_id.discount_amount',
                 'wizard_id.prorate_basis', 'wizard_id.manual_basis', 'wizard_id.replace_existing',
                 'wizard_id.line_ids.include', 'wizard_id.line_ids.weight',
                 'wizard_id.line_ids.line_percent', 'wizard_id.line_ids.line_amount')
    def _compute_new_values(self):
        for line in self:
            line.new_discount = line.current_discount
            line.new_subtotal = 0.0
        for wizard in self.mapped('wizard_id'):
            if not wizard._get_document():
                continue
            try:
                plan = wizard._get_plan()
            except UserError:
                plan = False
            for line in self.filtered(lambda l: l.wizard_id == wizard):
                source = line.purchase_line_id or line.move_line_id
                info = plan and plan['info'].get(source.id)
                if info:
                    line.new_discount = info['new_d']
                    line.new_subtotal = info['new_net']
                elif source:
                    line.new_subtotal = source._apd_untaxed(source.discount)
