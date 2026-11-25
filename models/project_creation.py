from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError


_CREATION = object()


class ProjectCreationCategory(models.Model):
    _inherit = 'approval.category'

    acme_kind = fields.Selection(selection_add=[('project_creation', 'Project Creation Request')],
                                 ondelete={'project_creation': 'set null'})


class ProjectCreationRequest(models.Model):
    _inherit = 'approval.request'

    acme_creation_order_id = fields.Many2one(
        'sale.order', string='Agreed Quotation / BOQ or Contract Order', check_company=True, ondelete='restrict', copy=False,
    )

    def _acme_requires_financial_approval(self):
        return self.acme_kind != 'project_creation' and super()._acme_requires_financial_approval()

    def _check_acme_request(self):
        super(ProjectCreationRequest, self.filtered(lambda req: req.acme_kind != 'project_creation'))._check_acme_request()
        for request in self.filtered(lambda req: req.acme_kind == 'project_creation'):
            order = request.acme_creation_order_id
            if not order or order.company_id != request.company_id:
                raise UserError(_('Select an agreed source document in the request company.'))
            source = order.x_tender_id or order.opportunity_id
            if not source:
                raise UserError(_('Link the source document to a won Tender or CRM Opportunity.'))
            source._acme_check_won_project_source()
            source._acme_check_agreement_document(order)

    @api.model_create_multi
    def create(self, vals_list):
        requests = super().create(vals_list)
        if requests.filtered(lambda req: req.acme_kind == 'project_creation' and req.acme_project_id):
            raise AccessError(_('The project link is set upon final approval.'))
        return requests

    def _acme_can_link_approved_project(self):
        return (self.acme_kind == 'project_creation' and self.request_status == 'approved'
                and not self.acme_project_id and self.env.context.get('acme_creation_token') is _CREATION)

    def write(self, vals):
        locked = {'acme_creation_order_id', 'acme_project_id', 'category_id', 'request_owner_id', 'approver_ids'}
        if locked.intersection(vals) and self.env.context.get('acme_creation_token') is not _CREATION:
            category = self.env['approval.category'].browse(vals.get('category_id') or [])
            for req in self:
                if req.acme_kind == 'project_creation' or category.acme_kind == 'project_creation':
                    if req.date_confirmed or req.acme_project_id or vals.get('acme_project_id'):
                        raise AccessError(_('The submitted Project Creation Request and its project link are locked.'))
        return super().write(vals)

    def action_confirm(self):
        for req in self.filtered(lambda req: req.acme_kind == 'project_creation'):
            minimum = req.category_id.approval_minimum
            required = req.approver_ids.filtered('required')
            if (minimum < 1 or len(req.approver_ids) < minimum or not required
                    or req.request_owner_id in req.approver_ids.user_id):
                raise UserError(_('Configure independent approvers, at least one required approver, and a positive approval minimum.'))
        return super().action_confirm()

    def action_approve(self, approver=None):
        if not self.filtered(lambda req: req.acme_kind == 'project_creation'):
            return super().action_approve(approver=approver)
        self.ensure_one()
        self.check_access('write')
        self.env.cr.execute('UPDATE approval_request SET id = id WHERE id = %s', [self.id])
        self.invalidate_recordset()
        if self.request_status == 'approved' and self.acme_project_id:
            return self.action_acme_open_created_project()
        if (self.request_status != 'pending' or not self.approver_ids.filtered(
                lambda row: row.user_id == self.env.user and row.status == 'pending')):
            raise AccessError(_('Only a designated current approver can approve this request.'))
        request = self.with_context(acme_creation_token=_CREATION)
        result = super(ProjectCreationRequest, request).action_approve()
        if request.request_status == 'approved':
            order = request.acme_creation_order_id
            source = order.x_tender_id or order.opportunity_id
            if not source:
                raise UserError(_('The approved document must still belong to its won source.'))
            request.acme_project_id = source._acme_register_from_agreement(order)
        return result

    def _action_force_approval(self):
        if self.filtered(lambda req: req.acme_kind == 'project_creation'):
            raise UserError(_('Use the configured approvers to approve Project Creation Requests.'))
        return super()._action_force_approval()

    def action_acme_open_created_project(self):
        self.ensure_one()
        self.check_access('read')
        if self.acme_kind != 'project_creation' or self.request_status != 'approved' or not self.acme_project_id:
            raise UserError(_('A final approved Project Creation Request is required.'))
        return self.acme_creation_order_id._acme_registered_project_action(self.acme_project_id)


class ProjectCreationApprover(models.Model):
    _inherit = 'approval.approver'

    def _acme_check_creation_approver(self):
        if self.env.context.get('acme_creation_token') is not _CREATION and self.request_id.filtered(
            lambda req: req.acme_kind == 'project_creation' and req.date_confirmed
        ):
            raise AccessError(_('Submitted Project Creation approval routing is locked.'))

    @api.model_create_multi
    def create(self, vals_list):
        rows = super().create(vals_list)
        rows._acme_check_creation_approver()
        return rows

    def write(self, vals):
        if {'user_id', 'required', 'sequence', 'request_id'}.intersection(vals) or vals.get('status') == 'approved':
            self._acme_check_creation_approver()
            if vals.get('request_id') and self.env['approval.request'].browse(vals['request_id']).filtered(
                lambda req: req.acme_kind == 'project_creation' and req.date_confirmed
            ):
                raise AccessError(_('Submitted Project Creation approval routing is locked.'))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_creation_routing(self):
        self._acme_check_creation_approver()
