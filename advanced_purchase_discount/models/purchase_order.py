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
                 'order_line.price_subtotal', 'order_line.display_type')
    def _compute_apd_amounts(self):
        for order in self:
            lines = order.order_line.filtered(lambda l: not l.display_type)
            gross = sum(lines.filtered(lambda l: not l.apd_is_discount_line).mapped('apd_gross_subtotal'))
            net = sum(lines.mapped('price_subtotal'))
            order.apd_amount_gross = gross
            order.apd_amount_discount = order.currency_id.round(gross - net) if order.currency_id else gross - net

    def _compute_apd_application_count(self):
        data = self.env['apd.discount.application']._read_group(
            [('purchase_order_id', 'in', self.ids)], ['purchase_order_id'], ['purchase_order_id'])
        counts = {d['purchase_order_id'][0]: d['purchase_order_id_count'] for d in data}
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
            not l.display_type and not l.apd_is_discount_line and l.product_id
            and l.product_qty > 0 and l.price_unit > 0
            and float_is_zero(l.qty_invoiced, precision_rounding=l.product_uom.rounding or 0.001)
        ))

    def _apd_discount_lines(self):
        return self.order_line.filtered(lambda l: l.apd_is_discount_line and l.product_qty)

    def _apd_amount_untaxed(self):
        return self.currency_id.round(sum(self.order_line.filtered(lambda l: not l.display_type)
                                          .mapped('price_subtotal')))

    def _apd_check_editable(self):
        for order in self:
            order.company_id._apd_check_enabled()
            if order.state not in ('draft', 'sent', 'to approve', 'purchase'):
                raise UserError(_('Discounts can only be applied on RFQs and confirmed (unlocked) purchase orders.'))

    def _apd_apply_changes(self, changed, remove_lines, new_specs):
        self.ensure_one()
        lines_by_discount = {}
        for line_id, discount in changed.items():
            lines_by_discount.setdefault(discount, []).append(line_id)
        Line = self.env['purchase.order.line']
        for discount, ids in lines_by_discount.items():
            Line.browse(ids).write({'discount': discount})
        if remove_lines:
            invoiced = remove_lines.filtered(lambda l: l.qty_invoiced)
            if invoiced:
                raise UserError(_('Discount lines that are already billed cannot be removed: %s',
                                  ', '.join(invoiced.mapped('name'))))
            if self.state in ('purchase', 'done'):
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
                    'product_uom': (product.uom_po_id or product.uom_id).id,
                    'price_unit': spec['price_unit'],
                    'taxes_id': [(6, 0, spec['taxes'].ids)],
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

    discount = fields.Float(string='Disc. (%)', digits='Purchase Discount', default=0.0)
    apd_is_discount_line = fields.Boolean(string='Is Discount Line', readonly=True, copy=True,
                                          help='Dedicated discount line created by the discount wizard.')
    apd_gross_subtotal = fields.Monetary(
        string='Gross Subtotal', compute='_compute_apd_gross_subtotal', store=True,
        help='Untaxed amount of the line without discount.')

    _sql_constraints = [
        ('apd_discount_range', 'CHECK (discount >= 0 AND discount <= 100)',
         'The discount of a purchase order line must be between 0 and 100.'),
    ]

    @api.depends('product_qty', 'price_unit', 'taxes_id', 'discount')
    def _compute_amount(self):
        return super()._compute_amount()

    @api.depends('product_qty', 'price_unit', 'taxes_id', 'display_type')
    def _compute_apd_gross_subtotal(self):
        for line in self:
            line.apd_gross_subtotal = 0.0 if line.display_type else line._apd_untaxed(0.0)

    def _convert_to_tax_base_line_dict(self):
        res = super()._convert_to_tax_base_line_dict()
        discount = self.discount or 0.0
        if self.env.context.get('apd_net_price_unit'):
            # Valuation helpers read the price unit only: give them the net price.
            res['price_unit'] = res['price_unit'] * (1.0 - discount / 100.0)
            res['discount'] = 0.0
        else:
            res['discount'] = discount
        return res

    def _get_gross_price_unit(self):
        return super(PurchaseOrderLine, self.with_context(apd_net_price_unit=True))._get_gross_price_unit()

    def _get_stock_move_price_unit(self):
        return super(PurchaseOrderLine, self.with_context(apd_net_price_unit=True))._get_stock_move_price_unit()

    def _prepare_account_move_line(self, move=False):
        res = super()._prepare_account_move_line(move=move)
        if not self.display_type:
            res['discount'] = self.discount
            if self.apd_is_discount_line:
                res['apd_is_discount_line'] = True
        return res

    def _compute_qty_invoiced(self):
        super()._compute_qty_invoiced()
        for line in self.filtered('apd_is_discount_line'):
            if line.order_id.state in ('purchase', 'done'):
                line.qty_to_invoice = line.product_qty - line.qty_invoiced

    def _compute_price_unit_and_date_planned_and_name(self):
        super()._compute_price_unit_and_date_planned_and_name()
        for line in self:
            if line.apd_is_discount_line or not line.product_id or line.invoice_lines or not line.company_id:
                continue
            if not line.company_id.apd_auto_vendor_discount:
                continue
            seller = line._apd_seller()
            if seller:
                line.discount = seller.apd_discount

    def write(self, vals):
        res = super().write(vals)
        if 'discount' in vals:
            for line in self.filtered(lambda l: not l.display_type and l.order_id.state == 'purchase'):
                moves = line.move_ids.filtered(
                    lambda m: m.state not in ('cancel', 'done') and m.product_id == line.product_id)
                if moves:
                    moves.write({'price_unit': line._get_stock_move_price_unit()})
        return res

    # ------------------------------------------------------------------
    # Engine adapter
    # ------------------------------------------------------------------
    def _apd_currency(self):
        return self.order_id.currency_id or self.currency_id

    def _apd_untaxed(self, discount):
        """Untaxed amount of the line for a given discount, using the native tax engine."""
        self.ensure_one()
        base = self.with_context(apd_net_price_unit=False)._convert_to_tax_base_line_dict()
        base['discount'] = discount
        result = self.env['account.tax']._compute_taxes([base])
        totals = list(result['totals'].values())
        amount = totals[0]['amount_untaxed'] if totals else 0.0
        currency = self._apd_currency()
        return currency.round(amount) if currency else amount

    def _apd_taxes(self):
        return self.taxes_id

    def _apd_quantity(self):
        return self.product_qty

    def _apd_product_qty(self):
        if self.product_uom and self.product_id and self.product_uom != self.product_id.uom_id:
            return self.product_uom._compute_quantity(self.product_qty, self.product_id.uom_id)
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
            uom_id=line.product_uom,
            params=params)
