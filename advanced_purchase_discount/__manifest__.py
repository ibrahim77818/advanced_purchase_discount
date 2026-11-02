# -*- coding: utf-8 -*-
{
    'name': 'Advanced Purchase & Vendor Bill Discounts',
    'version': '19.0.1.0.0',
    'summary': 'Percentage / fixed discounts on Purchase Orders and Vendor Bills with '
               'proportional, weighted, dedicated-line and tiered volume distribution',
    'description': """
Advanced Purchase Discount
==========================
* Company-level switch and validated discount service product (Purchase settings).
* Discount wizard on RFQs / Purchase Orders and draft Vendor Bills.
* Percentage or fixed-amount input.
* Mode A: proportional proration (by amount, quantity or evenly).
* Mode B: manual weighted allocation, per-line percentages or per-line amounts.
* Mode C: dedicated negative discount line(s), one per tax group.
* Mode D: tiered volume discounts and vendor pricelist discounts.
* Exact allocation (largest remainder on currency units, verified against the tax engine).
* Line discounts flow PO -> receipt valuation -> vendor bill -> journal entries.
* Full audit log of every discount applied.
    """,
    'category': 'Inventory/Purchase',
    'author': 'Ibrahim Yahia',
    'website': 'https://www.linkedin.com/in/ibrahim-yahia',
    'license': 'LGPL-3',
    'depends': ['purchase_stock'],
    'data': [
        'security/apd_security.xml',
        'security/ir.model.access.csv',
        'data/apd_data.xml',
        'views/apd_discount_tier_views.xml',
        'views/apd_discount_application_views.xml',
        'views/res_config_settings_views.xml',
        'wizard/apd_discount_wizard_views.xml',
        'views/purchase_order_views.xml',
        'views/account_move_views.xml',
        'views/stock_picking_views.xml',
        'views/apd_menus.xml',
        'report/purchase_order_report.xml',
        'report/account_invoice_report.xml',
    ],
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
