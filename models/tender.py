from odoo.addons.acme_project_core.models.boq import _IMPORT_TOKEN
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

from .project import _AGREEMENT_REGISTRATION_TOKEN

class Tender(models.Model):
    _inherit = 'crm.lead'
    acme_project_ids = fields.One2many('project.project', 'acme_tender_id', string='Projects')


    acme_opportunity_project_ids = fields.One2many('project.project', 'acme_opportunity_id')
    acme_handover_quote_id = fields.Many2one(
        'sale.order', string='Agreed Quotation / BOQ', check_company=True, copy=False,
        help='Select the agreed document when more than one revision exists.',
    )
    acme_agreement_reference = fields.Char(string='Agreement / Contract Reference', copy=False)
    acme_handover_template_id = fields.Many2one(
        'project.project', string='Registration Project Template', check_company=True, copy=False,
        domain="[('is_template', '=', True)]",
    )
    acme_has_registered_project = fields.Boolean(compute='_compute_acme_has_registered_project')
    acme_is_won_stage = fields.Boolean(related='stage_id.is_won')

    @api.depends('acme_project_ids', 'acme_opportunity_project_ids', 'acme_handover_quote_id',
                 'acme_handover_quote_id.acme_registered_project_ids')
    def _compute_acme_has_registered_project(self):
        for lead in self:
            lead.acme_has_registered_project = bool(lead._acme_existing_project())

    def _acme_existing_project(self, document=None):
        self.ensure_one()
        lead = self.with_context(active_test=False)
        projects = lead.acme_project_ids | lead.acme_opportunity_project_ids
        document = document or lead.acme_handover_quote_id
        if document:
            projects |= self.env['project.project'].with_context(active_test=False).search([
                '|', ('acme_registration_quote_id', '=', document.id), ('acme_order_id', '=', document.id),
            ])
        return projects

    @api.model_create_multi
    def create(self, vals_list):
        if any({'acme_handover_quote_id', 'acme_agreement_reference', 'acme_handover_template_id'} & vals.keys()
               for vals in vals_list):
            self._acme_check_project_edm()
        return super().create(vals_list)

    def write(self, vals):
        if {'acme_handover_quote_id', 'acme_agreement_reference', 'acme_handover_template_id'} & vals.keys():
            self._acme_check_project_edm()
            if any(lead._acme_existing_project() for lead in self):
                raise UserError(_('The agreement selection is locked after project registration.'))
        return super().write(vals)

    @api.constrains('acme_handover_quote_id', 'company_id', 'partner_id')
    def _acme_validate_selected_quote(self):
        for lead in self.filtered('acme_handover_quote_id'):
            lead._acme_check_agreement_document(lead.acme_handover_quote_id)

    def _acme_check_won_project_source(self):
        self.ensure_one()
        self.check_access('read')
        won = self.x_outcome_type == 'won' if self.x_is_tender else self.type == 'opportunity' and self.stage_id.is_won
        if not won or not self.company_id or not self.partner_id:
            raise UserError(_('A won Tender or Opportunity with a company and customer is required.'))

    def _acme_check_project_edm(self):
        if not self.env.user.has_group('acme_project_core.group_acme_edm'):
            raise AccessError(_('Only an EDM can create or open a project from this source.'))

    def action_acme_register_tender_project(self):
        self._acme_check_project_edm()
        return self.env['sale.order']._acme_registered_project_action(self._acme_register_from_agreement())

    def action_acme_open_source_project(self):
        self._acme_check_project_edm()
        self._acme_check_won_project_source()
        project = self._acme_existing_project()
        if len(project) != 1:
            raise UserError(_('Exactly one registered project is required to open it.'))
        return self.env['sale.order']._acme_registered_project_action(project)

    def _acme_check_agreement_document(self, document):
        document.check_access('read')
        source = document.x_tender_id if self.x_is_tender else document.opportunity_id
        if (source != self or document.state not in ('draft', 'sent', 'sale')
                or document.company_id != self.company_id
                or document.partner_id.commercial_partner_id != self.partner_id.commercial_partner_id):
            raise UserError(_('Select an eligible quotation or BOQ for this source, company and customer.'))

    def _acme_register_from_agreement(self, document=None):
        self.check_access('write')
        self._acme_check_won_project_source()
        # Serialise all registration entry points on the originating record.
        self.env.cr.execute('UPDATE crm_lead SET id = id WHERE id = %s', [self.id])
        self.invalidate_recordset()
        self._acme_check_won_project_source()
        selected = self.acme_handover_quote_id
        if document:
            self._acme_check_agreement_document(document)
        projects = self._acme_existing_project(document)
        if len(projects) > 1:
            raise UserError(_('Multiple projects are already linked to this agreement. Resolve the duplicate links.'))
        if projects:
            projects.check_access('read')
            return projects
        if selected:
            self._acme_check_agreement_document(selected)
        if document and selected and document != selected:
            raise UserError(_('This order differs from the agreed project source. Open the won source to register it.'))
        document = document or selected
        if not document:
            documents = self.x_tender_order_ids if self.x_is_tender else self.order_ids
            documents = documents.filtered(lambda order: order.state in ('draft', 'sent', 'sale'))
            if len(documents) != 1:
                raise UserError(_('Select the Agreed Quotation / BOQ on the won record before creating a project.'))
            document = documents
        self._acme_check_agreement_document(document)
        lines = document.order_line.filtered(lambda line: not line.display_type and not line.is_downpayment)
        if not lines:
            raise UserError(_('The selected quotation or agreement has no BOQ item lines.'))
        templates = self.acme_handover_template_id or lines.product_id.project_template_id
        if len(templates) != 1 or not templates.is_template or templates.company_id not in (
            self.company_id, self.env['res.company'],
        ):
            raise UserError(_('Choose one project template for this company or configure it on the source products.'))
        templates.check_access('read')
        code = self.env['ir.sequence'].with_company(self.company_id).next_by_code('acme.project.code')
        if not code:
            raise UserError(_('Configure the ACME project registration sequence.'))
        return self._acme_create_registered_project(document, templates, code, lines)

    def _acme_create_registered_project(self, document, template, code, lines):
        customer = self.partner_id
        source_code = self.x_tender_code if self.x_is_tender else False
        boq_vat = self.x_boq_vat_rate if self.x_is_tender else (
            100 * document.amount_tax / document.amount_untaxed if document.amount_untaxed else 0
        )
        values = {
            'name': '[%s] %s' % (source_code, self.name) if source_code and source_code not in self.name else self.name,
            'company_id': self.company_id.id, 'partner_id': customer.id, 'account_id': False,
            'acme_tender_id': self.id if self.x_is_tender else False,
            'acme_opportunity_id': self.id if not self.x_is_tender else False,
            'acme_registration_quote_id': document.id,
            'acme_order_id': document.id if document.state == 'sale' else False,
            'acme_contract_reference': self.acme_agreement_reference or document.client_order_ref or source_code or document.name,
            'acme_contract_currency_id': document.currency_id.id, 'acme_contract_amount': document.amount_untaxed,
            'acme_contract_total': document.amount_total, 'acme_boq_vat_rate': boq_vat,
            'acme_cost_budget': document.x_total_built_up_cost if self.x_is_tender else 0,
            'acme_project_sequence': code,
        }
        project = template.with_company(self.company_id).with_context(
            acme_agreement_registration_token=_AGREEMENT_REGISTRATION_TOKEN,
        ).action_create_from_template(values)
        if not project.account_id:
            project._create_analytic_account()
        project._acme_set_registration_state('created')
        self.env['acme.project.boq'].with_context(acme_import_token=_IMPORT_TOKEN).create([
            {
                'project_id': project.id, 'source_line_id': line.id, 'sequence': line.sequence,
                'reference': line.x_item_ref, 'name': line.name, 'bill_no': line.x_bill_no,
                'category': line.x_bill_category_id.name, 'product_name': line.product_id.display_name,
                'quantity': line.product_uom_qty,
                'unit_rate': line.x_boq_unit_rate if self.x_is_tender else
                             (line.price_subtotal / line.product_uom_qty if line.product_uom_qty else 0),
                'selling_rate': line.price_subtotal / line.product_uom_qty if line.product_uom_qty else 0,
                'unit': line.product_uom_id.name,
            }
            for line in lines
        ])
        project.with_context(acme_import_token=_IMPORT_TOKEN).write({'acme_boq_initialized': True})
        project.message_post(body=_('Project registered from the agreed quotation / BOQ: %s.', document.display_name))
        return project
