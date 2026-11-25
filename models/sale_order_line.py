from odoo import models


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    def _timesheet_service_generation(self):
        ordinary_lines = self.filtered(lambda line: not line.order_id.x_tender_id and not (
            line.order_id.opportunity_id.stage_id.is_won and (
                line.order_id.opportunity_id.acme_handover_quote_id == line.order_id
                or line.order_id.opportunity_id.acme_opportunity_project_ids
            )
        ))
        return super(SaleOrderLine, ordinary_lines)._timesheet_service_generation()
