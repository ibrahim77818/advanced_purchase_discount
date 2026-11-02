# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ResCompany(models.Model):
    _inherit = 'res.company'

    apd_discount_enabled = fields.Boolean(
        string='Advanced Purchase Discounts',
        default=False,
        help='Enable the discount wizard on purchase orders and vendor bills of this company.',
    )
    apd_discount_product_id = fields.Many2one(
        'product.product',
        string='Discount Product',
        domain="[('detailed_type', '=', 'service'), ('purchase_ok', '=', True), "
               "'|', ('company_id', '=', False), ('company_id', '=', id)]",
        help='Service product used for dedicated discount lines (Mode C) and for discount tracking. '
             'Its expense account receives the discount posting.',
    )
    apd_auto_vendor_discount = fields.Boolean(
        string='Apply Vendor Agreement Discounts',
        default=False,
        help='When a vendor pricelist line carries an agreement discount, copy it on the purchase '
             'order line automatically when the vendor price is selected.',
    )

    @api.constrains('apd_discount_enabled', 'apd_discount_product_id')
    def _check_apd_discount_product(self):
        for company in self:
            if company.apd_discount_enabled and not company.apd_discount_product_id \
                    and not self.env.context.get('apd_defer_company_check'):
                raise ValidationError(_(
                    'Company "%s": a discount service product is required when Advanced Purchase '
                    'Discounts are enabled.', company.name))
            if company.apd_discount_product_id:
                company._apd_validate_discount_product(company.apd_discount_product_id)

    def _apd_validate_discount_product(self, product):
        """Account and tax validation of the discount product for this company."""
        self.ensure_one()
        company = self
        if product.detailed_type != 'service':
            raise ValidationError(_('The discount product "%s" must be a service product.', product.display_name))
        if not product.purchase_ok:
            raise ValidationError(_('The discount product "%s" must be purchasable.', product.display_name))
        if product.company_id and product.company_id != company:
            raise ValidationError(_(
                'The discount product "%(product)s" belongs to another company (%(company)s).',
                product=product.display_name, company=product.company_id.name))
        accounts = product.product_tmpl_id.with_company(company)._get_product_accounts()
        expense = accounts.get('expense')
        if not expense:
            raise ValidationError(_(
                'No expense account can be found for the discount product "%(product)s" in company '
                '"%(company)s". Set an expense account on the product or its category.',
                product=product.display_name, company=company.name))
        if expense.deprecated:
            raise ValidationError(_('The expense account "%s" of the discount product is deprecated.',
                                    expense.display_name))
        if expense.company_id != company:
            raise ValidationError(_(
                'The expense account "%(account)s" of the discount product does not belong to company '
                '"%(company)s".', account=expense.display_name, company=company.name))
        taxes = product.with_company(company).supplier_taxes_id.filtered(lambda t: t.company_id == company)
        wrong = taxes.filtered(lambda t: t.type_tax_use not in ('purchase', 'none'))
        if wrong:
            raise ValidationError(_(
                'The vendor taxes of the discount product must be purchase taxes (%s).',
                ', '.join(wrong.mapped('name'))))
        return True

    def _apd_check_enabled(self, require_product=False):
        self.ensure_one()
        if not self.apd_discount_enabled:
            raise ValidationError(_(
                'Advanced Purchase Discounts are not enabled for company "%s" '
                '(Purchase > Configuration > Settings).', self.name))
        if require_product:
            if not self.apd_discount_product_id:
                raise ValidationError(_('Configure a discount product in the Purchase settings first.'))
            self._apd_validate_discount_product(self.apd_discount_product_id)
