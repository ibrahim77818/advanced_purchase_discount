==========================================
Advanced Purchase & Vendor Bill Discounts
==========================================

Odoo 19.0 (Community & Enterprise) — technical name ``advanced_purchase_discount``.
Depends on ``purchase_stock`` only. Arabic translation included (``i18n/ar.po``).

Configuration
=============
Purchase > Configuration > Settings > Invoicing > **Advanced Purchase Discounts** (per company).

* When enabled, a **Discount Product** is mandatory. It is validated on save: service type,
  purchasable, same company (or shared), resolvable and non-deprecated expense account of the
  company, purchase-type vendor taxes. A ready-made "Purchase Discount" service product is created
  on install (set its expense account, e.g. *Purchase discounts received*).
* Vendor agreement discounts use the native **Discount (%)** of vendor pricelists (Odoo 17), which
  Odoo copies on purchase order lines and which Mode D also evaluates.
* Purchase > Configuration > **Volume Discount Tiers**.

Usage
=====
A **Discount** button is shown on RFQs / purchase orders (draft, sent, to approve, purchase) and on
draft vendor bills and refunds. The wizard previews every line before anything is written.

==========  ============================================================================
Mode        Behaviour
==========  ============================================================================
A           Percentage applied to every selected line, or a fixed amount spread
            proportionally to the line amount, the quantity, or evenly.
B           Fixed/percentage total split by user weights, or a specific % or amount per line.
C           One negative line per tax group (discount product, tax-excluded amount prorated
            on the untaxed amount of each group, price-included taxes handled).
D           Best of vendor agreement discount and per-line volume tier, then the
            whole-document tier (prorated or as a dedicated line).
==========  ============================================================================

*Replace Existing Discounts* (default) resets line discounts and removes previous discount lines;
unchecked, the new discount is compounded on top (10% then 10% = 19%).
Fixed amounts are **tax excluded**. Every operation is stored in Purchase > Reporting >
**Discount Log** (before/after per line, residual, user, reason) and posted in the chatter.

Accounting design
=================
* Odoo 17 has a native ``discount`` on purchase order lines, used by the tax engine, the bill and
  the receipt valuation (``_get_gross_price_unit``). The module only raises its precision and, for
  lines discounted with the wizard, keeps the discount when the quantity or UoM changes (core Odoo 17
  would reset it from the vendor pricelist).
* Receipts are valued at the **net** price (Odoo 19 stores the value on the stock move), and the
  bill posts the goods on the stock valuation account at that same net cost: no price-difference
  line appears when the bill matches the PO.
* Bills created from the PO carry the same discount. A warning banner (with a one-click fix)
  appears when a bill line discount differs from its PO line.
* Mode C lines post the discount on the discount product expense account; the goods are valued at
  the gross price (classic "discount received" accounting). The discount line is billed in full
  on the first bill of the order.
* Lines already billed and down-payment lines are excluded from the wizard; posted bills and
  locked purchase orders cannot be changed.

Exactness (no rounding leakage)
===============================
Odoo 18 computes line subtotals from raw (unrounded) amounts and, with the *round globally*
tax rounding method, rounds per tax group. The engine therefore solves each line discount so that
the raw amount itself hits the cent target, which keeps fixed amounts exact with both rounding
methods.

Fixed amounts are allocated on integer currency units (largest remainder with caps), then each line
discount % is solved and **verified against Odoo's own computation** (Newton steps + local search on
the precision grid), carrying any sub-cent difference to the next line. For this the module adds the
decimal precision **Purchase Discount** (6 digits) used by purchase lines and journal item discounts;
reports print discounts without trailing zeros. Any residual that could not be absorbed is shown in
the wizard and in the log (it is 0 in practice).

Upgrading from 16.0 / 17.0 / 18.0
=================================
``migrations/17.0.1.0.0/post-migrate.py`` copies the 16.0 *Agreement Discount (%)*
(``product.supplierinfo.apd_discount``) into the native vendor pricelist ``discount``. The
*Apply Vendor Agreement Discounts* setting no longer exists: Odoo 17 always applies the
pricelist discount.
