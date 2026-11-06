from collections import defaultdict
from datetime import timedelta

from odoo import api, models

# 修改这些字段会影响员工的计划工时
LEAVE_FIELDS = {'date_from', 'date_to', 'resource_id', 'calendar_id', 'time_type', 'company_id'}


class ResourceCalendarLeaves(models.Model):
    """请假批准或取消、公共假日变化时，重新计算受影响员工的每日考勤。

    Odoo 批准请假时会建一条 resource.calendar.leaves，取消时删除；公共假日也是这里的记录（不指定员工）。
    """
    _inherit = 'resource.calendar.leaves'

    def _zk_queue_recompute(self):
        if not self:
            return
        Day = self.env['kairlyn.zk.attendance.day']
        leaves = self.sudo()
        employees = self.env['hr.employee'].sudo().with_context(active_test=False).search(
            [('resource_id', 'in', leaves.resource_id.ids)]) if leaves.resource_id else self.env['hr.employee']
        by_resource = {employee.resource_id.id: employee for employee in employees}
        employees_by_date = defaultdict(lambda: self.env['hr.employee'])
        for leave in leaves.filtered('date_from'):
            if leave.resource_id:
                targets = by_resource.get(leave.resource_id.id, self.env['hr.employee'])
            else:
                targets = Day._get_target_employees(leave.company_id or None)
                if leave.calendar_id:
                    targets = targets.filtered(lambda e: e.resource_calendar_id == leave.calendar_id)
            employees_by_date[leave.date_from.date() - timedelta(days=1)] |= targets
        for date_from, targets in employees_by_date.items():
            if targets:
                Day._queue_recompute(targets, date_from)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._zk_queue_recompute()
        return records

    def write(self, vals):
        if not LEAVE_FIELDS & set(vals):
            return super().write(vals)
        self._zk_queue_recompute()
        res = super().write(vals)
        self._zk_queue_recompute()
        return res

    def unlink(self):
        self._zk_queue_recompute()
        return super().unlink()
