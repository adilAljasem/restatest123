import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    # ------------------------------------------------------------------------------------------------
    # A new restaurant cannot be onboarded with ZATCA at once (commercial registration, VAT number and
    # an OTP from the Fatoora portal are needed). Standard Odoo then refuses to open the point of sale
    # and to post its invoices. Until the onboarding, the sales journal issues phase-1 simplified
    # invoices (QR code, no ZATCA submission); phase 2 (ZATCA e-invoicing) is switched on by itself
    # on the journal as soon as the onboarding gives it its production certificate.
    # ------------------------------------------------------------------------------------------------
    def _l10n_sa_zatca_format(self):
        return self.env.ref('l10n_sa_edi.edi_sa_zatca', raise_if_not_found=False)

    @api.model
    def _l10n_sa_hold_zatca_until_onboarded(self):
        """Sales journals of Saudi companies that are not onboarded yet: no ZATCA e-invoicing for now."""
        zatca = self._l10n_sa_zatca_format()
        if not zatca:
            return self.browse()
        journals = self.sudo().search([('type', '=', 'sale'), ('edi_format_ids', 'in', zatca.ids)])
        held = journals.filtered(lambda j: j.country_code == 'SA' and not j.l10n_sa_production_csid_json
                                 and not self.env['account.edi.document'].search_count(
                                     [('move_id.journal_id', '=', j.id), ('state', 'in', ('to_send', 'to_cancel'))]))
        if held:
            held.write({'edi_format_ids': [(3, zatca.id)]})
            _logger.info("ZATCA: journals %s issue phase-1 invoices until their onboarding",
                         ", ".join(held.mapped('name')))
        return held

    def write(self, vals):
        if self.env.context.get('l10n_sa_async_keep_chain_head') and 'l10n_sa_latest_submission_hash' in vals:
            vals = dict(vals)
            vals.pop('l10n_sa_latest_submission_hash')
            if not vals:
                return True
        res = super().write(vals)
        if vals.get('l10n_sa_production_csid_json'):
            # onboarding done: ZATCA e-invoicing (phase 2) on from now on
            zatca = self._l10n_sa_zatca_format()
            onboarded = self.filtered(lambda j: zatca and j.type == 'sale' and zatca not in j.edi_format_ids)
            if onboarded:
                onboarded.sudo().write({'edi_format_ids': [(4, zatca.id)]})
        return res

    def _l10n_sa_get_last_posted_invoice(self):
        """Standard Odoo blocks an invoice when the highest-ICV invoice of the journal is not yet accepted,
        because it signs and submits at the same time. With local signing, later invoices legitimately exist
        before earlier ones are reported, so only block on an earlier *synchronous* invoice that timed out."""
        move_id = self.env.context.get('l10n_sa_async_move_id')
        if not move_id:
            return super()._l10n_sa_get_last_posted_invoice()
        self.ensure_one()
        move = self.env['account.move'].browse(move_id)
        previous_sync = self.env['account.move'].search([
            ('journal_id', '=', self.id),
            ('l10n_sa_chain_index', '!=', 0),
            ('l10n_sa_chain_index', '<', move.l10n_sa_chain_index),
            ('l10n_sa_async_signed', '=', False),
        ], limit=1, order='l10n_sa_chain_index desc')
        if previous_sync and not previous_sync._l10n_sa_is_in_chain():
            return previous_sync
        return move

