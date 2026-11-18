from odoo import api, fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    pos_has_logo = fields.Boolean(
        compute='_compute_pos_has_logo',
        help="The restaurant has its own logo (not empty, not the «Your logo» placeholder). "
             "Without it the receipt prints the restaurant's name instead of an image.",
    )

    def _compute_pos_has_logo(self):
        placeholder = self._get_logo()
        for company in self:
            logo = company.sudo().with_context(bin_size=False).logo
            company.pos_has_logo = bool(logo) and logo != placeholder

    @api.model
    def _load_pos_data_fields(self, config):
        return super()._load_pos_data_fields(config) + ['pos_has_logo']
