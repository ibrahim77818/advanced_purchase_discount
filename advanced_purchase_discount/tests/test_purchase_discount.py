# -*- coding: utf-8 -*-
from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, tagged

from odoo.addons.stock_account.tests.test_anglo_saxon_valuation_reconciliation_common import (
    ValuationReconciliationTestCommon,
)


@tagged('post_install', '-at_install')
class TestAdvancedPurchaseDiscount(ValuationReconciliationTestCommon):

    @classmethod
    def setUpClass(cls, chart_template_ref=None):
        super().setUpClass(chart_template_ref=chart_template_ref)
        cls.company = cls.company_data['company']
        cls.currency = cls.company.currency_id
        cls.tax_15 = cls.company_data['default_tax_purchase']
        cls.tax_5 = cls.env['account.tax'].create({
            'name': 'Purchase 5%', 'amount': 5.0, 'type_tax_use': 'purchase', 'company_id': cls.company.id,
        })
        cls.tax_15_incl = cls.env['account.tax'].create({
            'name': 'Purchase 15% incl', 'amount': 15.0, 'type_tax_use': 'purchase',
            'price_include': True, 'company_id': cls.company.id,
        })
        cls.discount_product = cls.env.ref('advanced_purchase_discount.product_purchase_discount')
        cls.discount_product.property_account_expense_id = cls.company_data['default_account_expense']
        cls.company.write({
            'apd_discount_enabled': True,
            'apd_discount_product_id': cls.discount_product.id,
        })
        cls.vendor = cls.partner_a
        cls.product_a = cls.test_product_order       # storable, fifo, real time
        cls.product_b = cls.test_product_delivery    # storable, fifo, real time
        cls.product_c = cls.env['product.product'].create({
            'name': 'Consumable C', 'type': 'consu', 'purchase_method': 'purchase',
        })

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _po(self, lines, partner=None, fpos=None):
        return self.env['purchase.order'].create({
            'partner_id': (partner or self.vendor).id,
            'fiscal_position_id': fpos and fpos.id or False,
            'order_line': [Command.create({
                'product_id': product.id,
                'name': product.name,
                'product_qty': qty,
                'price_unit': price,
                'taxes_id': [Command.set(taxes.ids)],
            }) for product, qty, price, taxes in lines],
        })

    def _wizard(self, doc, **vals):
        key = 'default_purchase_order_id' if doc._name == 'purchase.order' else 'default_move_id'
        if 'discount_value' in vals:
            value = vals.pop('discount_value')
            vals['discount_amount' if vals.get('discount_type') == 'fixed' else 'discount_percent'] = value
        wizard = self.env['apd.discount.wizard'].with_context(**{key: doc.id}).create(vals)
        return wizard

    def _apply(self, doc, **vals):
        wizard = self._wizard(doc, **vals)
        wizard.action_apply()
        return wizard

    def _gross(self, doc):
        lines = doc.order_line if doc._name == 'purchase.order' else doc.invoice_line_ids
        return sum(lines.filtered(lambda l: not l.apd_is_discount_line).mapped('apd_gross_subtotal'))

    # ------------------------------------------------------------------
    # 1. configuration
    # ------------------------------------------------------------------
    def test_01_company_settings_validation(self):
        company = self.env['res.company'].create({'name': 'APD Other Co'})
        with self.assertRaises(ValidationError):
            company.write({'apd_discount_enabled': True})
        storable = self.product_a
        with self.assertRaises(ValidationError):
            self.company.write({'apd_discount_product_id': storable.id})
        # settings wizard writes the related fields one by one: must not fail midway
        settings = self.env['res.config.settings'].create({
            'apd_discount_enabled': True,
            'apd_discount_product_id': self.discount_product.id,
        })
        settings.execute()
        self.assertTrue(self.company.apd_discount_enabled)

    def test_02_disabled_company_blocks_wizard(self):
        po = self._po([(self.product_a, 1, 100.0, self.tax_15)])
        self.company.apd_discount_enabled = False
        with self.assertRaises(ValidationError):
            po.action_apd_open_discount_wizard()

    # ------------------------------------------------------------------
    # 2. Mode A
    # ------------------------------------------------------------------
    def test_10_mode_a_percent(self):
        po = self._po([(self.product_a, 3, 100.0, self.tax_15), (self.product_b, 2, 50.0, self.tax_15)])
        self._apply(po, mode='prorate', discount_type='percent', discount_value=10.0)
        self.assertEqual(po.order_line.mapped('discount'), [10.0, 10.0])
        self.assertAlmostEqual(po.amount_untaxed, 360.0)
        self.assertAlmostEqual(po.amount_tax, 54.0)
        self.assertAlmostEqual(po.apd_amount_gross, 400.0)
        self.assertAlmostEqual(po.apd_amount_discount, 40.0)
        self.assertEqual(len(po.apd_application_ids), 1)

    def test_11_mode_a_fixed_exact_no_leakage(self):
        po = self._po([
            (self.product_a, 3, 33.33, self.tax_15),
            (self.product_b, 7, 17.89, self.tax_5),
            (self.product_c, 1, 1234.56, self.tax_15),
            (self.product_c, 13, 0.37, self.env['account.tax']),
        ])
        gross = po.amount_untaxed
        for basis in ('amount', 'quantity', 'equal'):
            wizard = self._apply(po, mode='prorate', discount_type='fixed', discount_value=123.45,
                                 prorate_basis=basis)
            self.assertAlmostEqual(po.amount_untaxed, round(gross - 123.45, 2), places=2, msg=basis)
            self.assertFalse(po.apd_application_ids[0].residual, basis)
            # PO tax totals agree with the tax engine
            self.assertAlmostEqual(po.amount_total, po.tax_totals['amount_total'], places=2)

    def test_12_mode_a_price_included_tax(self):
        po = self._po([(self.product_a, 4, 115.0, self.tax_15_incl), (self.product_b, 1, 57.5, self.tax_15_incl)])
        self.assertAlmostEqual(po.amount_untaxed, 450.0)
        self._apply(po, mode='prorate', discount_type='fixed', discount_value=45.0)
        self.assertAlmostEqual(po.amount_untaxed, 405.0)
        self.assertAlmostEqual(po.amount_tax, 60.75)

    def test_13_stacking_without_replace(self):
        po = self._po([(self.product_a, 1, 200.0, self.tax_15)])
        self._apply(po, mode='prorate', discount_type='percent', discount_value=10.0)
        self._apply(po, mode='prorate', discount_type='percent', discount_value=10.0, replace_existing=False)
        self.assertAlmostEqual(po.order_line.discount, 19.0)
        self._apply(po, mode='prorate', discount_type='percent', discount_value=5.0)
        self.assertAlmostEqual(po.order_line.discount, 5.0)

    def test_14_too_high_discount(self):
        po = self._po([(self.product_a, 1, 10.0, self.tax_15)])
        with self.assertRaises(UserError):
            self._apply(po, mode='prorate', discount_type='fixed', discount_value=11.0)

    # ------------------------------------------------------------------
    # 3. Mode B
    # ------------------------------------------------------------------
    def test_20_mode_b_weights(self):
        po = self._po([(self.product_a, 1, 1000.0, self.tax_15), (self.product_b, 1, 1000.0, self.tax_15)])
        wizard = self._wizard(po, mode='manual', manual_basis='weight', discount_type='fixed', discount_value=100.0)
        wizard.line_ids[0].weight = 3.0
        wizard.line_ids[1].weight = 1.0
        wizard.action_apply()
        line_a, line_b = po.order_line
        self.assertAlmostEqual(line_a.price_subtotal, 925.0)
        self.assertAlmostEqual(line_b.price_subtotal, 975.0)

    def test_21_mode_b_line_percent_and_amount(self):
        po = self._po([(self.product_a, 2, 100.0, self.tax_15), (self.product_b, 1, 80.0, self.tax_15)])
        wizard = self._wizard(po, mode='manual', manual_basis='line_percent')
        wizard.line_ids[0].line_percent = 12.5
        wizard.line_ids[1].line_percent = 0.0
        wizard.action_apply()
        self.assertEqual(po.order_line.mapped('discount'), [12.5, 0.0])
        wizard = self._wizard(po, mode='manual', manual_basis='line_amount')
        wizard.line_ids[0].line_amount = 13.37
        wizard.line_ids[1].line_amount = 0.01
        wizard.action_apply()
        self.assertAlmostEqual(po.order_line[0].price_subtotal, 186.63)
        self.assertAlmostEqual(po.order_line[1].price_subtotal, 79.99)

    # ------------------------------------------------------------------
    # 4. Mode C
    # ------------------------------------------------------------------
    def test_30_mode_c_one_line_per_tax_group(self):
        po = self._po([
            (self.product_a, 1, 600.0, self.tax_15),
            (self.product_b, 1, 400.0, self.tax_5),
            (self.product_c, 1, 100.0, self.tax_15),
        ])
        self._apply(po, mode='discount_line', discount_type='fixed', discount_value=110.0)
        disc_lines = po.order_line.filtered('apd_is_discount_line')
        self.assertEqual(len(disc_lines), 2)
        by_tax = {l.taxes_id: l.price_subtotal for l in disc_lines}
        self.assertAlmostEqual(by_tax[self.tax_15], -70.0)
        self.assertAlmostEqual(by_tax[self.tax_5], -40.0)
        self.assertAlmostEqual(po.amount_untaxed, 990.0)
        self.assertAlmostEqual(po.amount_tax, 630 * 0.15 + 360 * 0.05)
        # replace removes previous discount lines
        self._apply(po, mode='discount_line', discount_type='percent', discount_value=10.0)
        self.assertEqual(len(po.order_line.filtered('apd_is_discount_line')), 2)
        self.assertAlmostEqual(po.amount_untaxed, 990.0)

    def test_31_mode_c_price_included_group(self):
        po = self._po([(self.product_a, 1, 230.0, self.tax_15_incl)])
        self._apply(po, mode='discount_line', discount_type='fixed', discount_value=20.0)
        self.assertAlmostEqual(po.amount_untaxed, 180.0)
        disc = po.order_line.filtered('apd_is_discount_line')
        self.assertAlmostEqual(disc.price_subtotal, -20.0)

    def test_32_mode_c_billed_and_posted(self):
        po = self._po([(self.product_c, 2, 500.0, self.tax_15)])
        self._apply(po, mode='discount_line', discount_type='fixed', discount_value=100.0)
        po.button_confirm()
        disc = po.order_line.filtered('apd_is_discount_line')
        self.assertEqual(disc.qty_to_invoice, 1.0)
        po.action_create_invoice()
        bill = po.invoice_ids
        bill.invoice_date = fields.Date.today()
        self.assertAlmostEqual(bill.amount_untaxed, 900.0)
        self.assertAlmostEqual(bill.amount_tax, 135.0)
        disc_bill = bill.invoice_line_ids.filtered('apd_is_discount_line')
        self.assertEqual(disc_bill.account_id, self.company_data['default_account_expense'])
        bill.action_post()
        self.assertEqual(bill.state, 'posted')
        self.assertEqual(disc.qty_invoiced, 1.0)

    def test_33_mode_c_keeps_fiscal_position_taxes(self):
        fpos = self.env['account.fiscal.position'].create({
            'name': 'FP 15->5', 'company_id': self.company.id,
            'tax_ids': [Command.create({'tax_src_id': self.tax_15.id, 'tax_dest_id': self.tax_5.id})],
        })
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id, 'fiscal_position_id': fpos.id,
            'order_line': [Command.create({'product_id': self.product_c.id, 'product_qty': 1})],
        })
        po.order_line.write({'price_unit': 100.0, 'taxes_id': [Command.set(fpos.map_tax(self.tax_15).ids)]})
        self._apply(po, mode='discount_line', discount_type='percent', discount_value=10.0)
        disc = po.order_line.filtered('apd_is_discount_line')
        self.assertEqual(disc.taxes_id, self.tax_5)

    # ------------------------------------------------------------------
    # 5. Mode D
    # ------------------------------------------------------------------
    def test_40_mode_d_tiers_and_agreement(self):
        Tier = self.env['apd.discount.tier']
        Tier.create({'name': '5% from 10 units', 'scope': 'line', 'threshold_type': 'qty',
                     'min_threshold': 10, 'discount_percent': 5.0})
        Tier.create({'name': '8% from 50 units', 'scope': 'line', 'threshold_type': 'qty',
                     'min_threshold': 50, 'discount_percent': 8.0})
        Tier.create({'name': '2% above 1000', 'scope': 'order', 'threshold_type': 'amount',
                     'min_threshold': 1000, 'discount_percent': 2.0, 'apply_as': 'prorate'})
        self.env['product.supplierinfo'].create({
            'partner_id': self.vendor.id, 'product_tmpl_id': self.product_b.product_tmpl_id.id,
            'price': 10.0, 'min_qty': 1, 'apd_discount': 7.0,
        })
        po = self._po([(self.product_a, 60, 20.0, self.tax_15), (self.product_b, 12, 10.0, self.tax_15),
                       (self.product_c, 1, 5.0, self.tax_15)])
        self._apply(po, mode='tiered')
        line_a, line_b, line_c = po.order_line
        # line a: 8% tier then 2% order tier compounded
        self.assertAlmostEqual(line_a.discount, 9.84)
        # line b: max(agreement 7%, tier 5%) then 2%
        self.assertAlmostEqual(line_b.discount, 8.86)
        # line c: no line tier, only the document tier
        self.assertAlmostEqual(line_c.discount, 2.0)

    def test_41_mode_d_document_tier_as_discount_line(self):
        self.env['apd.discount.tier'].create({
            'name': '3% above 100', 'scope': 'order', 'threshold_type': 'amount',
            'min_threshold': 100, 'discount_percent': 3.0, 'apply_as': 'discount_line'})
        po = self._po([(self.product_a, 10, 20.0, self.tax_15)])
        self._apply(po, mode='tiered')
        self.assertAlmostEqual(po.amount_untaxed, 194.0)
        self.assertTrue(po.order_line.filtered('apd_is_discount_line'))

    def test_42_auto_vendor_agreement_discount(self):
        self.company.apd_auto_vendor_discount = True
        self.env['product.supplierinfo'].create({
            'partner_id': self.vendor.id, 'product_tmpl_id': self.product_a.product_tmpl_id.id,
            'price': 50.0, 'min_qty': 1, 'apd_discount': 4.0,
        })
        po_form = Form(self.env['purchase.order'])
        po_form.partner_id = self.vendor
        with po_form.order_line.new() as line:
            line.product_id = self.product_a
            line.product_qty = 2
        po = po_form.save()
        self.assertAlmostEqual(po.order_line.discount, 4.0)
        self.assertAlmostEqual(po.amount_untaxed, 96.0)

    # ------------------------------------------------------------------
    # 6. lifecycle: PO -> receipt valuation -> bill -> journal entries
    # ------------------------------------------------------------------
    def test_50_lifecycle_valuation_and_bill(self):
        po = self._po([(self.product_a, 3, 33.33, self.tax_15), (self.product_b, 7, 17.89, self.tax_15)])
        self._apply(po, mode='prorate', discount_type='fixed', discount_value=25.0)
        net = po.amount_untaxed
        po.button_confirm()
        picking = po.picking_ids
        for move in picking.move_ids:
            move.quantity_done = move.product_uom_qty
        picking.button_validate()
        layers = picking.move_ids.stock_valuation_layer_ids
        self.assertAlmostEqual(sum(layers.mapped('value')), net, places=2)

        po.action_create_invoice()
        bill = po.invoice_ids
        bill.invoice_date = fields.Date.today()
        self.assertEqual(bill.invoice_line_ids.mapped('discount'), po.order_line.mapped('discount'))
        self.assertFalse(bill.apd_has_discount_mismatch)
        self.assertAlmostEqual(bill.amount_untaxed, net)
        self.assertAlmostEqual(bill.amount_total, po.amount_total)
        bill.action_post()
        # no price difference entry, interim accounts reconciled
        self.assertFalse(bill.line_ids.filtered(lambda l: l.account_id == self.company_data['default_account_expense']
                                                and l.product_id in (self.product_a | self.product_b)))
        interim = self.company_data['default_account_stock_in']
        interim_lines = (bill.line_ids | picking.move_ids.account_move_ids.line_ids).filtered(
            lambda l: l.account_id == interim)
        self.assertTrue(interim_lines)
        self.assertTrue(all(interim_lines.mapped('reconciled')), 'interim account fully reconciled')
        self.assertAlmostEqual(sum(interim_lines.mapped('balance')), 0.0, places=2)
        self.assertEqual(bill.apd_application_count, 1)

    def test_51_discount_change_after_confirmation_updates_moves(self):
        po = self._po([(self.product_a, 2, 100.0, self.tax_15)])
        po.button_confirm()
        self._apply(po, mode='prorate', discount_type='percent', discount_value=25.0)
        self.assertAlmostEqual(po.picking_ids.move_ids.price_unit, 75.0)

    def test_52_billed_lines_are_locked(self):
        po = self._po([(self.product_c, 1, 100.0, self.tax_15), (self.product_c, 1, 50.0, self.tax_15)])
        po.button_confirm()
        po.action_create_invoice()
        with self.assertRaises(UserError):
            self._apply(po, mode='prorate', discount_type='percent', discount_value=10.0)

    # ------------------------------------------------------------------
    # 7. vendor bills
    # ------------------------------------------------------------------
    def test_60_bill_modes_and_reset(self):
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': self.vendor.id, 'invoice_date': fields.Date.today(),
            'invoice_line_ids': [
                Command.create({'product_id': self.product_c.id, 'quantity': 3, 'price_unit': 33.33,
                                'tax_ids': [Command.set(self.tax_15.ids)]}),
                Command.create({'name': 'Freight', 'quantity': 1, 'price_unit': 71.11,
                                'account_id': self.company_data['default_account_expense'].id,
                                'tax_ids': [Command.set(self.tax_5.ids)]}),
            ],
        })
        gross = bill.amount_untaxed
        self._apply(bill, mode='prorate', discount_type='fixed', discount_value=17.17)
        self.assertAlmostEqual(bill.amount_untaxed, round(gross - 17.17, 2))
        self._apply(bill, mode='discount_line', discount_type='percent', discount_value=10.0)
        self.assertEqual(len(bill._apd_discount_lines()), 2)
        self.assertAlmostEqual(bill.amount_untaxed, round(gross * 0.9, 2), places=2)
        wizard = self._wizard(bill)
        wizard.action_reset()
        self.assertAlmostEqual(bill.amount_untaxed, gross)
        self.assertFalse(bill._apd_discount_lines())
        bill.action_post()
        self.assertEqual(bill.state, 'posted')
        with self.assertRaises(UserError):
            self._wizard(bill)

    def test_61_bill_refund(self):
        refund = self.env['account.move'].create({
            'move_type': 'in_refund', 'partner_id': self.vendor.id, 'invoice_date': fields.Date.today(),
            'invoice_line_ids': [Command.create({'product_id': self.product_c.id, 'quantity': 1,
                                                 'price_unit': 200.0, 'tax_ids': [Command.set(self.tax_15.ids)]})],
        })
        self._apply(refund, mode='discount_line', discount_type='fixed', discount_value=20.0)
        self.assertAlmostEqual(refund.amount_untaxed, 180.0)
        refund.action_post()

    def test_62_bill_mismatch_sync(self):
        po = self._po([(self.product_c, 1, 100.0, self.tax_15)])
        self._apply(po, mode='prorate', discount_type='percent', discount_value=10.0)
        po.button_confirm()
        po.action_create_invoice()
        bill = po.invoice_ids
        bill.invoice_line_ids.filtered('purchase_line_id').discount = 0.0
        self.assertTrue(bill.apd_has_discount_mismatch)
        bill.action_apd_sync_po_discounts()
        self.assertFalse(bill.apd_has_discount_mismatch)
        self.assertAlmostEqual(bill.amount_untaxed, 90.0)

    # ------------------------------------------------------------------
    # 8. preview
    # ------------------------------------------------------------------
    def test_70_wizard_preview_does_not_write(self):
        po = self._po([(self.product_a, 1, 100.0, self.tax_15)])
        wizard = self._wizard(po, mode='prorate', discount_type='percent', discount_value=20.0)
        self.assertAlmostEqual(wizard.amount_after, 80.0)
        self.assertAlmostEqual(wizard.line_ids.new_discount, 20.0)
        self.assertEqual(po.order_line.discount, 0.0)
        wizard.discount_type = 'fixed'
        wizard.discount_amount = 1000.0
        self.assertTrue(wizard.preview_error)

    def test_71_wizard_through_form_view(self):
        po = self._po([(self.product_a, 2, 100.0, self.tax_15), (self.product_b, 1, 100.0, self.tax_15)])
        wiz_form = Form(self.env['apd.discount.wizard'].with_context(default_purchase_order_id=po.id))
        wiz_form.mode = 'manual'
        wiz_form.manual_basis = 'weight'
        wiz_form.discount_type = 'fixed'
        wiz_form.discount_amount = 30.0
        with wiz_form.line_ids.edit(1) as line:
            line.weight = 2.0
        self.assertAlmostEqual(wiz_form.amount_after, 270.0)
        wizard = wiz_form.save()
        wizard.action_apply()
        self.assertAlmostEqual(po.order_line[0].price_subtotal, 190.0)
        self.assertAlmostEqual(po.order_line[1].price_subtotal, 80.0)
