from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    acme_registered_project_ids = fields.One2many('project.project', 'acme_order_id')
    acme_has_project = fields.Boolean(compute='_compute_acme_has_project')

    @api.depends('acme_registered_project_ids', 'x_tender_id.acme_project_ids',
                 'opportunity_id.acme_opportunity_project_ids', 'order_line.project_id', 'project_id')
    def _compute_acme_has_project(self):
        for order in self.with_context(active_test=False):
            source = order.x_tender_id or order.opportunity_id
            order.acme_has_project = bool(
                order.acme_registered_project_ids or (source and source._acme_existing_project(order))
                or order.project_id or order.order_line.project_id
            )

    def _acme_is_sales_project_order(self):
        self.ensure_one()
        return bool(not ('acme_addendum_variation_id' in self._fields and self.acme_addendum_variation_id)
                    and not self.x_tender_id
                    and 'x_sales_type' in self._fields and self.x_sales_type == 'project'
                    and self.opportunity_id and self.opportunity_id._acme_uses_sales_project_flow())

    def action_confirm(self):
        mixed = self.filtered(lambda order: order.state in ('draft', 'sent') and order.x_tender_id
                              and 'x_sales_type' in order._fields and order.x_sales_type == 'project'
                              and order.opportunity_id and 'x_is_project_sales' in order.opportunity_id._fields
                              and order.opportunity_id.x_is_project_sales)
        if mixed:
            raise UserError(_('Choose either the traditional Tender route or the CRM Project Sales route for the original contract; the order currently uses both.'))
        orders = self.filtered(lambda order: order.state in ('draft', 'sent') and order._acme_is_sales_project_order())
        if orders:
            leads = orders.opportunity_id.sorted('id')
            if len(leads) != len(orders):
                raise UserError(_('Confirm only one original Project Sales contract per opportunity. Use the authorized variation/addendum workflow for changes.'))
            # Same lock order as ACME registration. Upstream Sales CRM writes
            # the originating opportunity and order links in this transaction.
            for lead in leads:
                lead.check_access('read')
                self.env.cr.execute('UPDATE crm_lead SET id = id WHERE id = %s', [lead.id])
            self.env.invalidate_all()
            for order in orders:
                lead = order.opportunity_id
                # Read-only duplicate detection must include projects hidden by record rules.
                projects = lead.sudo()._acme_existing_project(order.sudo())
                if len(projects) > 1 or (projects and (order.sudo().x_project_id != projects or projects.x_sales_order_id != order.sudo())):
                    raise UserError(_('This opportunity already has a project for a different or unregistered agreement. Resolve the existing project links before confirming another original contract.'))
        return super().action_confirm()

    def action_acme_create_project(self):
        self.ensure_one()
        self.check_access('write')
        if not self.env.user.has_group('acme_project_core.group_acme_edm'):
            raise AccessError(_('Only an EDM can register a project.'))
        return self._acme_registered_project_action(self._acme_get_or_register_project())

    def _acme_check_registration_source(self):
        self.ensure_one()
        self.check_access('read')
        source = self.x_tender_id or self.opportunity_id
        if self.state != 'sale' or not source:
            raise UserError(_('Select a confirmed Sales Order from a won Tender or CRM Opportunity.'))
        source._acme_check_won_project_source()
        source._acme_check_agreement_document(self)

    def _acme_get_or_register_project(self):
        self.ensure_one()
        self.check_access('write')
        self._acme_check_registration_source()
        source = self.x_tender_id or self.opportunity_id
        # The originating record lock also protects the approved Request path.
        if (self.project_id or self.order_line.project_id) and not source._acme_existing_project(self):
            raise UserError(_('This order already has a project created by another workflow.'))
        return source._acme_register_from_agreement(self)

    def _acme_registered_project_action(self, project):
        project.check_access('read')
        return {'type': 'ir.actions.act_window', 'res_model': 'project.project',
                'res_id': project.id, 'view_mode': 'form'}
