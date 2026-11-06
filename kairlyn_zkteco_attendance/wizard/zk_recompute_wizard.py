from datetime import timedelta

from odoo import _, fields, models
from odoo.exceptions import UserError


class KairlynZkRecomputeWizard(models.TransientModel):
    """手动重新计算每日考勤结果。

    修改员工的工作时间表、补录以前的休假或公共假日后使用。
    最近 7 天的结果每小时会自动重算，更早的日子需要在这里手动重算。
    """
    _name = 'kairlyn.zk.recompute.wizard'
    _description = '重新计算 ZKTeco 每日考勤'

    employee_ids = fields.Many2many(
        'hr.employee', string='员工', context={'active_test': False},
        help='留空表示全部已关联考勤机用户的员工。')
    date_from = fields.Date(
        '起始日期', required=True,
        default=lambda self: self.env.company.zk_start_date or fields.Date.today() - timedelta(days=30),
        help='从这一天起重新计算到今天。早于“考勤计算起始日期”或在锁定日期之前的日子不会计算。')

    def action_recompute(self):
        self.ensure_one()
        if not self.env.user.has_group('hr_attendance.group_hr_attendance_manager'):
            raise UserError(_('只有考勤管理员可以重新计算考勤。'))
        Day = self.env['kairlyn.zk.attendance.day']
        employees = self.employee_ids or Day._get_target_employees(self.env.companies)
        without_start = employees.company_id.filtered(lambda c: not c.zk_start_date)
        if without_start:
            raise UserError(_(
                '公司 %s 还没有设置“考勤计算起始日期”，请先在 考勤 → 配置 → 设置 中设置。',
                ', '.join(without_start.mapped('name'))))
        Day._queue_recompute(employees, self.date_from)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('已加入计算队列'),
                'message': _('%s 名员工的每日考勤将在后台重新计算，稍后刷新页面查看。', len(employees)),
                'type': 'info',
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
