# -*- coding: utf-8 -*-
"""Discount computation engine shared by purchase orders and vendor bills.

The engine never writes on the documents: it builds a *plan* (new line discounts,
discount lines to add or remove) that the wizard previews and then applies.

Exactness strategy
------------------
* Amounts are allocated on integer currency units with the largest-remainder
  method, with per-line caps (a line can never be discounted below zero).
* Each line target (net untaxed amount) is converted into a discount percentage
  which is then *verified against Odoo's own tax engine* (``_compute_taxes`` for
  purchase lines, the ``_compute_totals`` formula for bill lines) and corrected by
  Newton steps and a local search on the precision grid.
* Any remaining difference is carried to the next line (largest lines first, so
  the finest-grained lines absorb it). A non-zero final residual is reported.
"""
import math

from odoo import _, api, models
from odoo.exceptions import UserError
from odoo.tools import float_round, float_compare, float_is_zero


class ApdDiscountEngine(models.AbstractModel):
    _name = 'apd.discount.engine'
    _description = 'Purchase Discount Engine'

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def _digits(self):
        return self.env['decimal.precision'].precision_get('Purchase Discount')

    @api.model
    def _round_pct(self, value):
        value = min(max(value, 0.0), 100.0)
        return float_round(value, precision_digits=self._digits())

    @api.model
    def _compound(self, current, extra):
        """Apply ``extra`` % on top of an existing ``current`` % discount."""
        return self._round_pct(100.0 - (100.0 - current) * (100.0 - extra) / 100.0)

    @api.model
    def _allocate(self, total, weights, caps, currency):
        """Distribute ``total`` proportionally to ``weights`` without exceeding ``caps``.

        Works on integer currency units (largest remainder) so that the sum of the
        allocations is exactly ``total``.
        """
        unit = currency.rounding
        n = len(weights)
        total_u = int(round(total / unit))
        caps_u = [max(int(round(c / unit)), 0) for c in caps]
        if total_u < 0:
            raise UserError(_('The discount amount cannot be negative.'))
        if total_u > sum(caps_u):
            raise UserError(_(
                'The discount (%(discount)s) is higher than the eligible amount (%(eligible)s).',
                discount=currency.round(total), eligible=currency.round(sum(caps_u) * unit)))
        alloc = [0] * n
        active = [i for i in range(n) if weights[i] > 0 and caps_u[i] > 0]
        remaining = total_u
        while remaining > 0:
            if not active:
                raise UserError(_('The discount cannot be distributed: all eligible lines have a zero weight.'))
            wsum = float(sum(weights[i] for i in active))
            raw = {i: remaining * weights[i] / wsum for i in active}
            capped = [i for i in active if alloc[i] + raw[i] >= caps_u[i]]
            if capped:
                for i in capped:
                    remaining -= caps_u[i] - alloc[i]
                    alloc[i] = caps_u[i]
                    active.remove(i)
                continue
            floors = {i: int(math.floor(raw[i])) for i in active}
            for i in active:
                alloc[i] += floors[i]
            left = remaining - sum(floors.values())
            order = sorted(active, key=lambda i: (raw[i] - floors[i], weights[i], -i), reverse=True)
            for i in order:
                if left <= 0:
                    break
                if alloc[i] < caps_u[i]:
                    alloc[i] += 1
                    left -= 1
            remaining = left
            active = [i for i in active if alloc[i] < caps_u[i]]
        return [float_round(a * unit, precision_rounding=unit) for a in alloc]

    @api.model
    def _solve_discount(self, line, gross, target):
        """Return the discount % that makes the line untaxed amount equal ``target``."""
        currency = line._apd_currency()
        digits = self._digits()
        if float_compare(target, gross, precision_rounding=currency.rounding) >= 0:
            return 0.0
        if float_compare(target, 0.0, precision_rounding=currency.rounding) <= 0:
            return 100.0
        d = self._round_pct((1.0 - target / gross) * 100.0)
        cache = {}

        def achieved(pct):
            if pct not in cache:
                cache[pct] = line._apd_untaxed(pct)
            return cache[pct]

        for _i in range(8):
            diff = achieved(d) - target
            if currency.is_zero(diff):
                return d
            new_d = self._round_pct(d + diff / gross * 100.0)
            if new_d == d:
                break
            d = new_d
        step = 10 ** -digits
        best = (abs(achieved(d) - target), d)
        for k in range(1, 40):
            for cand in (d + k * step, d - k * step):
                cand = self._round_pct(cand)
                diff = abs(achieved(cand) - target)
                if currency.is_zero(diff):
                    return cand
                if diff < best[0]:
                    best = (diff, cand)
        return best[1]

    @api.model
    def _solve_discount_line_price(self, doc, taxes, amount):
        """Price unit (qty 1) of a discount line whose untaxed amount is ``-amount``."""
        currency = doc._apd_currency()
        product = doc._apd_company().apd_discount_product_id
        partner = doc._apd_partner()
        is_refund = doc._apd_is_refund()
        digits = self.env['decimal.precision'].precision_get('Product Price')
        target = -amount

        def untaxed(price):
            if not taxes:
                return currency.round(price)
            return taxes.compute_all(price, currency=currency, quantity=1.0, product=product,
                                     partner=partner, is_refund=is_refund)['total_excluded']

        price = target
        if taxes.filtered('price_include'):
            ref = untaxed(target)
            if ref:
                price = target * target / ref
        price = float_round(price, precision_digits=digits)
        for _i in range(8):
            diff = untaxed(price) - target
            if currency.is_zero(diff):
                return price, 0.0
            ratio = (untaxed(price) / price) if price else 1.0
            new_price = float_round(price - diff / (ratio or 1.0), precision_digits=digits)
            if new_price == price:
                break
            price = new_price
        step = 10 ** -digits
        best = (abs(untaxed(price) - target), price)
        for k in range(1, 40):
            for cand in (price + k * step, price - k * step):
                cand = float_round(cand, precision_digits=digits)
                diff = abs(untaxed(cand) - target)
                if currency.is_zero(diff):
                    return cand, 0.0
                if diff < best[0]:
                    best = (diff, cand)
        return best[1], untaxed(best[1]) - target

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------
    @api.model
    def _build_plan(self, doc, params):
        """Compute what has to change on ``doc``.

        ``params`` keys: mode, discount_type, value, prorate_basis, manual_basis,
        replace, lines ({line_id: {'include', 'weight', 'percent', 'amount'}}).
        """
        currency = doc._apd_currency()
        company = doc._apd_company()
        mode = params['mode']
        replace = params.get('replace', True)
        dtype = params.get('discount_type', 'percent')
        value = params.get('value', 0.0) or 0.0
        line_params = params.get('lines') or {}

        eligible = doc._apd_eligible_lines()
        info = {}
        for line in eligible:
            gross = line._apd_untaxed(0.0)
            cur_d = 0.0 if replace else line.discount
            cur_net = gross if replace else line._apd_untaxed(cur_d)
            lp = line_params.get(line.id, {})
            info[line.id] = {
                'line': line,
                'gross': gross,
                'old_d': line.discount,
                'old_net': line._apd_untaxed(line.discount),
                'cur_d': cur_d,
                'cur_net': cur_net,
                'new_d': cur_d,
                'include': lp.get('include', True),
                'weight': lp.get('weight', 1.0),
                'percent': lp.get('percent', 0.0),
                'amount': lp.get('amount', 0.0),
            }
        included = [v for v in info.values() if v['include']]
        plan = {
            'info': info,
            'remove_lines': doc._apd_discount_lines() if replace else doc._apd_discount_lines().browse(),
            'new_discount_lines': [],
            'requested': 0.0,
            'residual': 0.0,
            'notes': [],
        }
        if mode == 'reset':
            for v in info.values():
                v['new_d'] = 0.0
            plan['remove_lines'] = doc._apd_discount_lines()
            return self._finalize(doc, plan)

        if value < 0:
            raise UserError(_('The discount value cannot be negative.'))
        if dtype == 'percent' and value > 100:
            raise UserError(_('A percentage discount cannot exceed 100%.'))
        if not included and mode != 'tiered':
            raise UserError(_('Select at least one eligible line.'))

        def total_from_value():
            base = sum(v['cur_net'] for v in included)
            if dtype == 'percent':
                return currency.round(base * value / 100.0)
            return currency.round(value)

        if mode == 'prorate':
            if dtype == 'percent':
                for v in included:
                    v['new_d'] = self._compound(v['cur_d'], value)
            else:
                basis = params.get('prorate_basis') or 'amount'
                if basis == 'amount':
                    weights = [v['cur_net'] for v in included]
                elif basis == 'quantity':
                    weights = [v['line']._apd_quantity() for v in included]
                else:
                    weights = [1.0] * len(included)
                self._apply_amounts(plan, included, total_from_value(), weights, currency)
        elif mode == 'manual':
            basis = params.get('manual_basis') or 'weight'
            if basis == 'weight':
                weights = [max(v['weight'], 0.0) for v in included]
                self._apply_amounts(plan, included, total_from_value(), weights, currency)
            elif basis == 'line_percent':
                for v in included:
                    if v['percent'] < 0 or v['percent'] > 100:
                        raise UserError(_('Line percentages must be between 0 and 100.'))
                    v['new_d'] = self._compound(v['cur_d'], v['percent'])
            else:
                targets = []
                for v in included:
                    amount = currency.round(v['amount'])
                    if amount < 0:
                        raise UserError(_('Line amounts cannot be negative.'))
                    if float_compare(amount, v['cur_net'], precision_rounding=currency.rounding) > 0:
                        raise UserError(_('The discount of line "%s" is higher than its amount.',
                                          v['line']._apd_label()))
                    targets.append(amount)
                self._solve_targets(plan, included, targets, currency)
        elif mode == 'discount_line':
            company._apd_check_enabled(require_product=True)
            self._add_discount_lines(doc, plan, included, total_from_value(), currency)
        elif mode == 'tiered':
            self._plan_tiered(doc, plan, included, currency)
        else:
            raise UserError(_('Unknown distribution mode.'))
        return self._finalize(doc, plan)

    @api.model
    def _apply_amounts(self, plan, included, total, weights, currency):
        caps = [v['cur_net'] for v in included]
        allocations = self._allocate(total, weights, caps, currency)
        self._solve_targets(plan, included, allocations, currency)

    @api.model
    def _solve_targets(self, plan, included, allocations, currency):
        """Turn per-line discount amounts into exact discount percentages."""
        plan['requested'] += sum(allocations)
        pairs = sorted(zip(included, allocations), key=lambda p: p[0]['gross'], reverse=True)
        carry = 0.0  # discount still owed (positive) or exceeded (negative)
        for v, amount in pairs:
            desired = amount + carry
            target = currency.round(v['cur_net'] - desired)
            target = min(max(target, 0.0), v['gross'])
            d = self._solve_discount(v['line'], v['gross'], target)
            v['new_d'] = d
            got = v['cur_net'] - v['line']._apd_untaxed(d)
            carry = currency.round(desired - got)
        plan['residual'] += currency.round(carry)

    @api.model
    def _add_discount_lines(self, doc, plan, included, total, currency, label=None):
        if currency.is_zero(total):
            return
        groups = {}
        for v in included:
            taxes = v['line']._apd_taxes()
            key = tuple(sorted(taxes.ids))
            groups.setdefault(key, {'taxes': taxes, 'net': 0.0})
            groups[key]['net'] += self._net_after(v)
        keys = list(groups)
        allocations = self._allocate(total, [groups[k]['net'] for k in keys],
                                     [groups[k]['net'] for k in keys], currency)
        plan['requested'] += total
        product = doc._apd_company().apd_discount_product_id
        for key, amount in zip(keys, allocations):
            if currency.is_zero(amount):
                continue
            taxes = groups[key]['taxes']
            price, residual = self._solve_discount_line_price(doc, taxes, amount)
            plan['residual'] += currency.round(residual)
            tax_label = ', '.join(taxes.mapped('name')) or _('No Tax')
            plan['new_discount_lines'].append({
                'taxes': taxes,
                'price_unit': price,
                'amount': amount,
                'name': '%s (%s)' % (label or product.with_context(lang=doc._apd_lang()).display_name, tax_label),
            })

    @api.model
    def _net_after(self, v):
        if v['new_d'] == v['cur_d']:
            return v['cur_net']
        return v['line']._apd_untaxed(v['new_d'])

    @api.model
    def _plan_tiered(self, doc, plan, included, currency):
        company = doc._apd_company()
        partner = doc._apd_partner()
        date = doc._apd_date()
        tiers = self.env['apd.discount.tier'].search([('company_id', '=', company.id)])
        tiers = tiers._apd_filter_valid(partner, date)
        line_tiers = tiers.filtered(lambda t: t.scope == 'line')
        order_tiers = tiers.filtered(lambda t: t.scope == 'order')
        applied = []
        for v in included:
            line = v['line']
            product = line.product_id
            qty = line._apd_product_qty()
            amount_company = doc._apd_to_company_currency(v['cur_net'])
            best = 0.0
            source = False
            seller = line._apd_seller()
            if seller and seller.apd_discount:
                best, source = seller.apd_discount, _('vendor agreement')
            for tier in line_tiers:
                if not tier._apd_match_product(product):
                    continue
                measure = qty if tier.threshold_type == 'qty' else amount_company
                if float_compare(measure, tier.min_threshold, precision_digits=6) >= 0 \
                        and tier.discount_percent > best:
                    best, source = tier.discount_percent, tier.name
            if best:
                v['new_d'] = self._compound(v['cur_d'], best)
                applied.append('%s: %s%% (%s)' % (line._apd_label(), best, source))
        # Whole-document tiers, evaluated after the line tiers
        if order_tiers and included:
            total_qty = sum(v['line']._apd_product_qty() for v in included)
            total_amount = doc._apd_to_company_currency(sum(self._net_after(v) for v in included))
            best_tier = False
            for tier in order_tiers:
                measure = total_qty if tier.threshold_type == 'qty' else total_amount
                if float_compare(measure, tier.min_threshold, precision_digits=6) >= 0:
                    if not best_tier or tier.discount_percent > best_tier.discount_percent:
                        best_tier = tier
            if best_tier:
                applied.append(_('Document tier "%(tier)s": %(pct)s%%', tier=best_tier.name,
                                 pct=best_tier.discount_percent))
                if best_tier.apply_as == 'prorate':
                    for v in included:
                        v['new_d'] = self._compound(v['new_d'], best_tier.discount_percent)
                else:
                    company._apd_check_enabled(require_product=True)
                    base = sum(self._net_after(v) for v in included)
                    total = currency.round(base * best_tier.discount_percent / 100.0)
                    self._add_discount_lines(doc, plan, included, total, currency)
        if not applied:
            plan['notes'].append(_('No vendor agreement or volume tier matched this document.'))
        plan['notes'] += applied

    @api.model
    def _finalize(self, doc, plan):
        currency = doc._apd_currency()
        changed = {}
        for line_id, v in plan['info'].items():
            v['new_net'] = v['old_net'] if v['new_d'] == v['old_d'] else self._net_after(v)
            if v['new_d'] != v['old_d']:
                changed[line_id] = v['new_d']
        plan['changed'] = changed
        removed_amount = sum(l._apd_untaxed(l.discount) for l in plan['remove_lines'])
        added_amount = -sum(spec['amount'] for spec in plan['new_discount_lines'])
        delta_lines = sum(v['new_net'] - v['old_net'] for v in plan['info'].values())
        plan['amount_before'] = doc._apd_amount_untaxed()
        plan['amount_after'] = currency.round(plan['amount_before'] + delta_lines - removed_amount + added_amount)
        plan['amount_discount'] = currency.round(plan['amount_before'] - plan['amount_after'])
        return plan

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------
    @api.model
    def _apply_plan(self, doc, plan, params, note=False):
        doc._apd_check_editable()
        currency = doc._apd_currency()
        details = []
        for line_id, new_d in plan['changed'].items():
            v = plan['info'][line_id]
            details.append((0, 0, {
                'description': v['line']._apd_label()[:256],
                'product_id': v['line'].product_id.id,
                'action': 'reset' if params['mode'] == 'reset' else 'line_discount',
                'discount_before': v['old_d'],
                'discount_after': new_d,
                'subtotal_before': v['old_net'],
                'subtotal_after': v['new_net'],
            }))
        for line in plan['remove_lines']:
            details.append((0, 0, {
                'description': line._apd_label()[:256],
                'product_id': line.product_id.id,
                'action': 'removed',
                'subtotal_before': line._apd_untaxed(line.discount),
                'subtotal_after': 0.0,
            }))
        for spec in plan['new_discount_lines']:
            details.append((0, 0, {
                'description': spec['name'][:256],
                'product_id': doc._apd_company().apd_discount_product_id.id,
                'action': 'discount_line',
                'subtotal_before': 0.0,
                'subtotal_after': -spec['amount'],
            }))
        doc._apd_apply_changes(plan['changed'], plan['remove_lines'], plan['new_discount_lines'])
        amount_after = doc._apd_amount_untaxed()
        app_vals = {
            'company_id': doc._apd_company().id,
            'currency_id': currency.id,
            'partner_id': doc._apd_partner().id,
            'mode': params['mode'],
            'discount_type': params.get('discount_type') if params['mode'] != 'reset' else False,
            'discount_value': params.get('value') or 0.0,
            'replace_existing': params.get('replace', True),
            'amount_before': plan['amount_before'],
            'amount_after': amount_after,
            'amount_discount': currency.round(plan['amount_before'] - amount_after),
            'residual': plan['residual'],
            'note': '\n'.join([n for n in ([note] + plan['notes']) if n]) or False,
            'line_ids': details,
        }
        app_vals.update(doc._apd_application_link())
        application = self.env['apd.discount.application'].sudo().create(app_vals)
        doc._apd_post_message(application)
        return application
