from odoo import api, fields, models


class PosConfig(models.Model):
    _inherit = 'pos.config'

    l10n_sa_zatca_async = fields.Boolean(
        string="Report ZATCA in background",
        default=True,
        help="Simplified (B2C) invoices are signed and given their QR code instantly at checkout, "
             "then reported to ZATCA in the background (ZATCA allows reporting within 24 hours). "
             "Standard (B2B) invoices still go through real-time clearance.",
    )
    l10n_sa_zatca_pending = fields.Boolean(
        string="ZATCA onboarding pending", compute="_compute_l10n_sa_zatca_pending",
        help="The invoice journal of this point of sale is not onboarded with ZATCA (Fatoora) yet: "
             "it issues phase-1 simplified invoices (QR code) until the onboarding is done.",
    )

    @api.depends('company_id', 'invoice_journal_id')
    def _compute_l10n_sa_zatca_pending(self):
        for config in self:
            journal = config.invoice_journal_id
            config.l10n_sa_zatca_pending = bool(
                config.company_id.country_id.code == 'SA'
                and journal
                and not journal.sudo().l10n_sa_production_csid_json
            )
