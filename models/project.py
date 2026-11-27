from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError, UserError
from odoo.addons.acme_project_approvals.models.approval import _GUARANTEE_SYNC


_REGISTRATION_TOKEN = object()
_AGREEMENT_REGISTRATION_TOKEN = object()
_APPROVED_REGISTRATION_CHANGE = object()
_REGISTRATION_FIELDS = {
    'acme_project_sequence', 'acme_order_id', 'acme_tender_id', 'acme_opportunity_id',
    'acme_registration_quote_id', 'company_id', 'partner_id',
    'acme_contract_currency_id', 'acme_contract_amount',
    'acme_contract_total', 'date_start', 'date', 'acme_advance_received',
    'acme_recovery_percent', 'acme_guarantee_ids', 'acme_bond_basis',
}


class ProjectTenderSource(models.Model):
    _inherit = 'project.project'

    acme_tender_id = fields.Many2one('crm.lead', string='Source Tender', copy=False, ondelete='restrict', check_company=True)
    acme_opportunity_id = fields.Many2one(
        'crm.lead', string='Source CRM Opportunity', copy=False, ondelete='restrict', check_company=True,
    )
    acme_registration_quote_id = fields.Many2one(
        'sale.order', string='Source Quotation / BOQ', copy=False, ondelete='restrict', check_company=True,
    )
    acme_project_source = fields.Selection(
        [('tender', 'Tender'), ('crm', 'CRM')], compute='_compute_acme_project_source', string='Project Source',
    )
    _acme_opportunity_unique = models.Constraint(
        'UNIQUE(acme_opportunity_id)', 'An opportunity can have only one linked project.',
    )
    _acme_quote_unique = models.Constraint(
        'UNIQUE(acme_registration_quote_id)', 'A source quotation can have only one registered project.',
    )
    _acme_tender_unique = models.Constraint('UNIQUE(acme_tender_id)', 'A tender can have only one linked project.')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if (vals.get('acme_registration_quote_id') or vals.get('acme_opportunity_id')) and (
                self.env.context.get('acme_agreement_registration_token') is not _AGREEMENT_REGISTRATION_TOKEN
            ):
                raise AccessError(_('Register won agreements using their project creation actions.'))
            if vals.get('acme_lifecycle_state', self.env.context.get('default_acme_lifecycle_state', 'draft')) != 'draft':
                raise UserError(_('Projects must start in Draft registration.'))
            if vals.get('acme_order_id'):
                order = self.env['sale.order'].browse(vals['acme_order_id'])
                order.check_access('read')
                if order.x_is_tender_quotation:
                    vals['acme_cost_budget'] = order.x_total_built_up_cost
        return super().create(vals_list)

    @api.depends('acme_tender_id', 'acme_opportunity_id')
    def _compute_acme_project_source(self):
        for project in self:
            project.acme_project_source = 'tender' if project.acme_tender_id else 'crm' if project.acme_opportunity_id else False

    @api.constrains('acme_order_id', 'acme_tender_id', 'acme_opportunity_id', 'acme_registration_quote_id')
    def _check_tender_contract(self):
        for project in self:
            for document in project.acme_order_id | project.acme_registration_quote_id:
                if project.acme_tender_id and document.x_tender_id != project.acme_tender_id:
                    raise ValidationError(_('The contract document must belong to the source Tender.'))
                if project.acme_opportunity_id and document.opportunity_id != project.acme_opportunity_id:
                    raise ValidationError(_('The contract document must belong to the source CRM Opportunity.'))

    def write(self, vals):
        if ({'acme_registration_quote_id', 'acme_opportunity_id'}.intersection(vals)
                and self.env.context.get('acme_agreement_registration_token') is not _AGREEMENT_REGISTRATION_TOKEN):
            raise AccessError(_('The originating agreement is set by project registration.'))
        if 'acme_lifecycle_state' in vals and self.env.context.get('acme_registration_token') is not _REGISTRATION_TOKEN:
            raise UserError(_('Use the project registration actions to change the registration state.'))
        registration = _REGISTRATION_FIELDS.intersection(vals)
        if registration == {'acme_guarantee_ids'} and all(
            command[0] == 1 and set(command[2]) <= _GUARANTEE_ISSUANCE
            and command[1] in self.acme_guarantee_ids.filtered('approval_id').ids
            for command in vals['acme_guarantee_ids']
        ):
            registration = set()
        if registration:
            self._acme_check_registration_unlocked()
        if {'acme_tender_id', 'acme_opportunity_id', 'acme_registration_quote_id'}.intersection(vals) and any(
            project.acme_boq_initialized for project in self
        ):
            raise UserError(_('The originating source cannot change after BOQ initialization.'))
        return super().write(vals)

    def _acme_check_registration_unlocked(self):
        if self.env.context.get('acme_approved_registration_change') is _APPROVED_REGISTRATION_CHANGE:
            return
        if self.filtered(lambda project: project.acme_lifecycle_state == 'verified'):
            raise UserError(_('Verified project registration is locked. Use an approved registration change.'))

    def _acme_set_registration_state(self, state):
        self.with_context(acme_registration_token=_REGISTRATION_TOKEN).write({'acme_lifecycle_state': state})

    def action_acme_verify_project(self):
        self.ensure_one()
        self.check_access('write')
        if not self.env.user.has_group('acme_project_core.group_acme_edm'):
            raise AccessError(_('Only an EDM can verify a project.'))
        if self.acme_lifecycle_state != 'created':
            raise UserError(_('Only a Created project can be verified.'))
        if (self.acme_opportunity_id and self.acme_opportunity_id._acme_uses_sales_project_flow()
                and not self.acme_boq_initialized):
            raise UserError(_('Load or finalize the contract BOQ before verifying this Project Sales project.'))
        self._acme_set_registration_state('verified')


_GUARANTEE_ISSUANCE = {'name', 'issue_date', 'expiry_date', 'scan', 'scan_filename'}


class ProjectGuarantee(models.Model):
    _inherit = 'acme.project.guarantee'

    @api.model_create_multi
    def create(self, vals_list):
        default_project = self.default_get(['project_id']).get('project_id')
        if self.env.context.get('acme_guarantee_sync') is not _GUARANTEE_SYNC:
            self.env['project.project'].browse([
                project_id for vals in vals_list
                if (project_id := vals.get('project_id', default_project))
            ])._acme_check_registration_unlocked()
        return super().create(vals_list)

    def write(self, vals):
        registration_fields = {
            'project_id', 'name', 'guarantee_type', 'bank_id', 'amount', 'issue_date',
            'expiry_date', 'scan', 'scan_filename', 'signed_contract', 'signed_contract_filename',
        }
        protected = self if set(vals).intersection(registration_fields) - _GUARANTEE_ISSUANCE else self.filtered(lambda record: not record.approval_id)
        if registration_fields.intersection(vals) and protected:
            projects = protected.project_id | self.env['project.project'].browse(vals.get('project_id') or [])
            projects._acme_check_registration_unlocked()
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_verified_registration(self):
        self.project_id._acme_check_registration_unlocked()
