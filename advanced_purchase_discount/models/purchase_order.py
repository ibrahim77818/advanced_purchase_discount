# -*- coding: utf-8 -*-
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_is_zero
from odoo.tools.misc import formatLang, get_lang


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    apd_discount_enabled = fields.Boolean(related='company_id.apd_discount_enabled')
    apd_amount_gross = fields.Monetary(
        string='Gross Amount', compute='_compute_apd_amounts', store=True,
        help='Untaxed amount of the lines before any discount.')
    apd_amount_discount = fields.Monetary(
        string='Total Discount', compute='_compute_apd_amounts', store=True,
        help='Line discounts plus dedicated discount lines (untaxed).')
    apd_application_ids = fields.One2many('apd.discount.application', 'purchase_order_id',
                                          string='Discount Log')
    apd_application_count = fields.Integer(compute='_compute_apd_application_count')

    @api.depends('order_line.apd_gross_subtotal', 'order_line.apd_is_discount_line',
                 'order_line.display_type', 'amount_untaxed')
    def _compute_apd_amounts(self):
        for order in self:
            lines = order.order_line.filtered(lambda l: not l.display_type)
            gross = sum(lines.filtered(lambda l: not l.apd_is_discount_line).mapped('apd_gross_subtotal'))
            # Odoo 18: line subtotals are raw (unrounded); the order total holds the rounded amount.
            net = order.amount_untaxed
            order.apd_amount_gross = gross
            order.apd_amount_discount = order.currency_id.round(gross - net) if order.currency_id else gross - net

    def _compute_apd_application_count(self):
        data = self.env['apd.discount.application']._read_group(
            [('purchase_order_id', 'in', self.ids)], ['purchase_order_id'], ['__count'])
        counts = {order.id: count for order, count in data}
        for order in self:
            order.apd_application_count = counts.get(order.id, 0)

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
            'context': {'default_purchase_order_id': self.id, 'dialog_size': 'extra-large'},
        }

    def action_apd_view_applications(self):
        self.ensure_one()
        action = self.env['ir.actions.actions']._for_xml_id(
            'advanced_purchase_discount.action_apd_discount_application')
        action['domain'] = [('purchase_order_id', '=', self.id)]
        action['context'] = {'create': False}
        return action

    # ------------------------------------------------------------------
    # Vendor pricelist creation on confirmation (Odoo 17)
    # ------------------------------------------------------------------
    def _prepare_supplier_info(self, partner, line, price, currency):
        # Odoo 17 copies the line discount in the new vendor pricelist line. A discount set by
        # the wizard is a one-off negotiation, not a vendor price agreement: do not persist it.
        vals = super()._prepare_supplier_info(partner, line, price, currency)
        if line.apd_discount_locked:
            vals['discount'] = 0.0
        return vals

    def _add_supplier_to_product(self):
        super()._add_supplier_to_product()
        # Dedicated discount lines must not register the vendor on the discount product with a
        # negative price.
        for order in self.filtered(lambda o: o.order_line.filtered('apd_is_discount_line')):
            product = order.order_line.filtered('apd_is_discount_line').product_id
            partners = order.partner_id | order.partner_id.commercial_partner_id
            product.sudo().seller_ids.filtered(
                lambda s: s.partner_id in partners and s.price < 0).unlink()

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
        return False

    def _apd_date(self):
        return (self.date_order or fields.Datetime.now()).date()

    def _apd_to_company_currency(self, amount):
        company = self.company_id
        return self.currency_id._convert(amount, company.currency_id, company, self._apd_date())

    def _apd_eligible_lines(self):
        return self.order_line.filtered(lambda l: (
            not l.display_type and not l.apd_is_discount_line and not l.is_downpayment and l.product_id
            and l.product_qty > 0 and l.price_unit > 0
            and float_is_zero(l.qty_invoiced, precision_rounding=l.product_uom_id.rounding or 0.001)
        ))

    def _apd_discount_lines(self):
        return self.order_line.filtered(lambda l: l.apd_is_discount_line and l.product_qty)

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
            currency_id=self.currency_id or self.company_id.currency_id,
            partner_id=self.partner_id,
            product_id=self.company_id.apd_discount_product_id,
            rate=self.currency_rate,
        )
        AccountTax._add_tax_details_in_base_line(base_line, self.company_id)
        return base_line['tax_details']['raw_total_excluded_currency']

    def _apd_check_editable(self):
        for order in self:
            order.company_id._apd_check_enabled()
            # Odoo 19: the 'done' state is replaced by the 'locked' flag.
            if order.state not in ('draft', 'sent', 'to approve', 'purchase') or order.locked:
                raise UserError(_('Discounts can only be applied on RFQs and confirmed (unlocked) purchase orders.'))

    def _apd_apply_changes(self, changed, remove_lines, new_specs):
        self.ensure_one()
        lines_by_discount = {}
        for line_id, discount in changed.items():
            lines_by_discount.setdefault(discount, []).append(line_id)
        Line = self.env['purchase.order.line']
        for discount, ids in lines_by_discount.items():
            Line.browse(ids).write({'discount': discount, 'apd_discount_locked': bool(discount)})
        if remove_lines:
            invoiced = remove_lines.filtered(lambda l: l.qty_invoiced)
            if invoiced:
                raise UserError(_('Discount lines that are already billed cannot be removed: %s',
                                  ', '.join(invoiced.mapped('name'))))
            if self.state == 'purchase':
                remove_lines.write({'product_qty': 0.0})
            else:
                remove_lines.unlink()
        if new_specs:
            product = self.company_id.apd_discount_product_id
            seq = max(self.order_line.mapped('sequence') or [10]) + 1
            vals_list = []
            for spec in new_specs:
                vals_list.append({
                    'order_id': self.id,
                    'sequence': seq,
                    'product_id': product.id,
                    'name': spec['name'],
                    'product_qty': 1.0,
                    'product_uom_id': product.uom_id.id,
                    'price_unit': spec['price_unit'],
                    'tax_ids': [(6, 0, spec['taxes'].ids)],
                    'date_planned': self.date_planned or fields.Datetime.now(),
                    'apd_is_discount_line': True,
                })
                seq += 1
            Line.create(vals_list)

    def _apd_application_link(self):
        return {'purchase_order_id': self.id}

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


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    # Odoo 17 ships a native ``discount`` field (computed from the vendor
    # pricelist, already used by the tax engine, the bill and the receipt
    # valuation). Only its precision is raised so that fixed amounts can be
    # distributed exactly to the cent.
    discount = fields.Float(digits='Purchase Discount')
    apd_is_discount_line = fields.Boolean(string='Is Discount Line', readonly=True, copy=True,
                                          help='Dedicated discount line created by the discount wizard.')
    apd_discount_locked = fields.Boolean(
        string='Discount Set by Wizard', readonly=True, copy=False,
        help='The discount was applied with the discount wizard: it is kept when the quantity or the '
             'unit of measure changes, instead of being reset from the vendor pricelist.')
    apd_gross_subtotal = fields.Monetary(
        string='Gross Subtotal', compute='_compute_apd_gross_subtotal', store=True,
        help='Untaxed amount of the line without discount.')

    _apd_discount_range = models.Constraint(
        'CHECK (discount >= 0 AND discount <= 100)',
        'The discount of a purchase order line must be between 0 and 100.',
    )

    @api.depends('product_qty', 'price_unit', 'tax_ids', 'display_type')
    def _compute_apd_gross_subtotal(self):
        for line in self:
            line.apd_gross_subtotal = 0.0 if line.display_type else line._apd_untaxed(0.0)

    def _compute_price_unit_and_date_planned_and_name(self):
        # Odoo 17 resets ``discount`` from the vendor pricelist whenever the quantity
        # or the UoM changes. Keep the discounts applied with the wizard.
        locked = {line: line.discount for line in self if line.apd_discount_locked}
        super()._compute_price_unit_and_date_planned_and_name()
        for line, discount in locked.items():
            line.discount = discount

    def _prepare_account_move_line(self, move=False):
        res = super()._prepare_account_move_line(move=move)
        if not self.display_type and self.apd_is_discount_line:
            res['apd_is_discount_line'] = True
        return res

    def _compute_qty_invoiced(self):
        super()._compute_qty_invoiced()
        for line in self.filtered('apd_is_discount_line'):
            if line.order_id.state == 'purchase':
                line.qty_to_invoice = line.product_qty - line.qty_invoiced

    # ------------------------------------------------------------------
    # Engine adapter
    # ------------------------------------------------------------------
    def _apd_currency(self):
        return self.order_id.currency_id or self.currency_id

    def _apd_untaxed_raw(self, discount):
        """Raw (unrounded) untaxed amount of the line for a given discount (Odoo 18 tax engine)."""
        self.ensure_one()
        base_line = self._prepare_base_line_for_taxes_computation()
        base_line['discount'] = discount
        self.env['account.tax']._add_tax_details_in_base_line(
            base_line, self.company_id or self.order_id.company_id)
        return base_line['tax_details']['raw_total_excluded_currency']

    def _apd_untaxed(self, discount):
        """Untaxed amount of the line for a given discount, rounded to the currency."""
        amount = self._apd_untaxed_raw(discount)
        currency = self._apd_currency()
        return currency.round(amount) if currency else amount

    def _apd_taxes(self):
        return self.tax_ids

    def _apd_quantity(self):
        return self.product_qty

    def _apd_product_qty(self):
        if self.product_uom_id and self.product_id and self.product_uom_id != self.product_id.uom_id:
            return self.product_uom_id._compute_quantity(self.product_qty, self.product_id.uom_id)
        return self.product_qty

    def _apd_label(self):
        return self.name or self.product_id.display_name or ''

    def _apd_seller(self):
        self.ensure_one()
        if not self.product_id:
            return self.env['product.supplierinfo']
        line = self.with_company(self.company_id)
        params = line._get_select_sellers_params()
        order_date = line.order_id.date_order
        return line.product_id._select_seller(
            partner_id=line.partner_id,
            quantity=line.product_qty,
            date=order_date and order_date.date() or fields.Date.context_today(line),
            uom_id=line.product_uom_id,
            params=params)
