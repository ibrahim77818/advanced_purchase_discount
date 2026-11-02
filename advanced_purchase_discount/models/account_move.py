# -*- coding: utf-8 -*-
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare
from odoo.tools.misc import formatLang, get_lang


class AccountMove(models.Model):
    _inherit = 'account.move'

    apd_discount_enabled = fields.Boolean(related='company_id.apd_discount_enabled')
    apd_amount_gross = fields.Monetary(
        string='Gross Amount', compute='_compute_apd_amounts', store=True,
        currency_field='currency_id', help='Untaxed amount of the lines before any discount.')
    apd_amount_discount = fields.Monetary(
        string='Total Discount', compute='_compute_apd_amounts', store=True,
        currency_field='currency_id', help='Line discounts plus dedicated discount lines (untaxed).')
    apd_has_discount_mismatch = fields.Boolean(compute='_compute_apd_has_discount_mismatch')
    apd_application_count = fields.Integer(compute='_compute_apd_application_count')

    @api.depends('invoice_line_ids.apd_gross_subtotal', 'amount_untaxed',
                 'invoice_line_ids.apd_is_discount_line', 'invoice_line_ids.display_type')
    def _compute_apd_amounts(self):
        for move in self:
            if not move.is_purchase_document(include_receipts=False):
                move.apd_amount_gross = move.apd_amount_discount = 0.0
                continue
            lines = move.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
            gross = sum(lines.filtered(lambda l: not l.apd_is_discount_line).mapped('apd_gross_subtotal'))
            # Odoo 18: line subtotals are raw (unrounded); the move total holds the rounded amount.
            net = move.amount_untaxed
            move.apd_amount_gross = gross
            move.apd_amount_discount = move.currency_id.round(gross - net) if move.currency_id else gross - net

    @api.depends('invoice_line_ids.apd_discount_mismatch')
    def _compute_apd_has_discount_mismatch(self):
        for move in self:
            move.apd_has_discount_mismatch = any(move.invoice_line_ids.mapped('apd_discount_mismatch'))

    def _apd_application_domain(self):
        self.ensure_one()
        orders = self.line_ids.purchase_line_id.order_id
        return ['|', ('move_id', '=', self.id), ('purchase_order_id', 'in', orders.ids)]

    def _compute_apd_application_count(self):
        Application = self.env['apd.discount.application']
        for move in self:
            move.apd_application_count = Application.search_count(move._apd_application_domain()) \
                if move.id and move.is_purchase_document() else 0

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_apd_open_discount_wizard(self):
        self.ensure_one()
        self._apd_check_editable()
        return {
            'name': _('Apply Discount'),
            'type': 'ir.actions.act_window',
            'res_model': 'apd.discount.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_move_id': self.id, 'dialog_size': 'extra-large'},
        }

    def action_apd_view_applications(self):
        self.ensure_one()
        action = self.env['ir.actions.actions']._for_xml_id(
            'advanced_purchase_discount.action_apd_discount_application')
        action['domain'] = self._apd_application_domain()
        action['context'] = {'create': False}
        return action

    def action_apd_sync_po_discounts(self):
        """Realign bill line discounts with their purchase order lines."""
        for move in self:
            move._apd_check_editable()
            commands = [(1, line.id, {'discount': line.purchase_line_id.discount})
                        for line in move.invoice_line_ids if line.apd_discount_mismatch]
            if commands:
                move.write({'invoice_line_ids': commands})
        return True

    # ------------------------------------------------------------------
    # Engine adapter
    # ------------------------------------------------------------------
    def _apd_company(self):
        return self.company_id

    def _apd_currency(self):
        return self.currency_id

    def _apd_partner(self):
        return self.partner_id

    def _apd_lang(self):
        return get_lang(self.env, self.partner_id.lang).code

    def _apd_is_refund(self):
        return self.move_type == 'in_refund'

    def _apd_date(self):
        return self.invoice_date or self.date or fields.Date.context_today(self)

    def _apd_to_company_currency(self, amount):
        company = self.company_id
        return self.currency_id._convert(amount, company.currency_id, company, self._apd_date())

    def _apd_eligible_lines(self):
        return self.invoice_line_ids.filtered(lambda l: (
            l.display_type == 'product' and not l.apd_is_discount_line
            and l.quantity > 0 and l.price_unit > 0
        ))

    def _apd_discount_lines(self):
        return self.invoice_line_ids.filtered(lambda l: l.display_type == 'product' and l.apd_is_discount_line)

    def _apd_amount_untaxed(self):
        return self.amount_untaxed

    def _apd_raw_untaxed_for(self, taxes, price_unit):
        """Raw untaxed amount of a hypothetical one-unit discount line (Odoo 18 tax engine)."""
        AccountTax = self.env['account.tax']
        base_line = AccountTax._prepare_base_line_for_taxes_computation(
            {},
            tax_ids=taxes,
            price_unit=price_unit,
            quantity=1.0,
            currency_id=self.currency_id,
            partner_id=self.partner_id,
            product_id=self.company_id.apd_discount_product_id,
            is_refund=self._apd_is_refund(),
        )
        AccountTax._add_tax_details_in_base_line(base_line, self.company_id)
        return base_line['tax_details']['raw_total_excluded_currency']

    def _apd_check_editable(self):
        for move in self:
            move.company_id._apd_check_enabled()
            if move.move_type not in ('in_invoice', 'in_refund'):
                raise UserError(_('Purchase discounts are only available on vendor bills and refunds.'))
            if move.state != 'draft':
                raise UserError(_('Discounts can only be applied on draft vendor bills.'))

    def _apd_apply_changes(self, changed, remove_lines, new_specs):
        self.ensure_one()
        commands = [(1, line_id, {'discount': discount}) for line_id, discount in changed.items()]
        commands += [(2, line.id) for line in remove_lines]
        if new_specs:
            product = self.company_id.apd_discount_product_id
            seq = max(self.invoice_line_ids.mapped('sequence') or [10]) + 1
            for spec in new_specs:
                commands.append((0, 0, {
                    'sequence': seq,
                    'product_id': product.id,
                    'name': spec['name'],
                    'quantity': 1.0,
                    'price_unit': spec['price_unit'],
                    'tax_ids': [(6, 0, spec['taxes'].ids)],
                    'apd_is_discount_line': True,
                }))
                seq += 1
        if commands:
            self.write({'invoice_line_ids': commands})

    def _apd_application_link(self):
        return {'move_id': self.id}

    def _apd_post_message(self, application):
        mode = dict(application._fields['mode']._description_selection(self.env)).get(application.mode)
        body = Markup('<p>%s</p><ul><li>%s: %s</li><li>%s: %s</li><li>%s: %s</li></ul>') % (
            escape(_('Discount applied (%s).', application.name)),
            escape(_('Mode')), escape(mode),
            escape(_('Untaxed before')),
            escape(formatLang(self.env, application.amount_before, currency_obj=application.currency_id)),
            escape(_('Untaxed after')),
            escape(formatLang(self.env, application.amount_after, currency_obj=application.currency_id)),
        )
        self.message_post(body=body)


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    # Higher precision so that fixed-amount distributions are exact (see README).
    discount = fields.Float(digits='Purchase Discount')
    apd_is_discount_line = fields.Boolean(string='Is Discount Line', copy=True, readonly=True)
    apd_gross_subtotal = fields.Monetary(
        string='Gross Subtotal', compute='_compute_apd_gross_subtotal', store=True,
        currency_field='currency_id')
    apd_po_discount = fields.Float(
        related='purchase_line_id.discount', string='PO Disc. (%)', digits='Purchase Discount')
    apd_discount_mismatch = fields.Boolean(compute='_compute_apd_discount_mismatch',
                                           string='Discount differs from PO')

    @api.depends('quantity', 'price_unit', 'tax_ids', 'display_type', 'currency_id')
    def _compute_apd_gross_subtotal(self):
        for line in self:
            if line.display_type != 'product' or not line.move_id.is_purchase_document(include_receipts=True):
                line.apd_gross_subtotal = 0.0
            else:
                line.apd_gross_subtotal = line._apd_untaxed(0.0)

    @api.depends('discount', 'purchase_line_id.discount', 'display_type')
    def _compute_apd_discount_mismatch(self):
        digits = self.env['decimal.precision'].precision_get('Purchase Discount')
        for line in self:
            line.apd_discount_mismatch = bool(
                line.display_type == 'product' and line.purchase_line_id
                and line.move_id.state == 'draft'
                and float_compare(line.discount, line.purchase_line_id.discount, precision_digits=digits) != 0
            )

    # ------------------------------------------------------------------
    # Engine adapter
    # ------------------------------------------------------------------
    def _apd_currency(self):
        return self.currency_id or self.move_id.currency_id

    def _apd_untaxed_raw(self, discount):
        """Raw untaxed amount for a given discount, as ``account.move.line._compute_totals`` (Odoo 18)."""
        self.ensure_one()
        base_line = self.move_id._prepare_product_base_line_for_taxes_computation(self)
        base_line['discount'] = discount
        self.env['account.tax']._add_tax_details_in_base_line(base_line, self.company_id or self.move_id.company_id)
        return base_line['tax_details']['raw_total_excluded_currency']

    def _apd_untaxed(self, discount):
        amount = self._apd_untaxed_raw(discount)
        currency = self._apd_currency()
        return currency.round(amount) if currency else amount

    def _apd_taxes(self):
        return self.tax_ids

    def _apd_quantity(self):
        return self.quantity

    def _apd_product_qty(self):
        if self.product_uom_id and self.product_id and self.product_uom_id != self.product_id.uom_id:
            return self.product_uom_id._compute_quantity(self.quantity, self.product_id.uom_id)
        return self.quantity

    def _apd_label(self):
        return self.name or self.product_id.display_name or ''

    def _apd_seller(self):
        self.ensure_one()
        if not self.product_id:
            return self.env['product.supplierinfo']
        move = self.move_id
        return self.product_id.with_company(move.company_id)._select_seller(
            partner_id=move.partner_id,
            quantity=self.quantity,
            date=move.invoice_date or move.date or fields.Date.context_today(self),
            uom_id=self.product_uom_id)
