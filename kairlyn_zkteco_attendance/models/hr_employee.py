from odoo import fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    zk_attendance_required = fields.Boolean(
        '参与考勤', default=True, groups='hr.group_hr_user',
        help='不参与考勤的人员仍会导入原始打卡记录，但生成考勤时不计迟到、早退和缺卡。')
    zk_device_user_ids = fields.One2many(
        'kairlyn.zk.device.user', 'employee_id', '考勤机用户',
        groups='hr_attendance.group_hr_attendance_user')
    zk_punch_count = fields.Integer(
        '打卡记录', compute='_compute_zk_punch_count',
        groups='hr_attendance.group_hr_attendance_user')
    zk_day_exception_count = fields.Integer(
        '考勤异常', compute='_compute_zk_day_exception_count',
        groups='hr_attendance.group_hr_attendance_user')

    def _compute_zk_punch_count(self):
        data = dict(self.env['kairlyn.zk.punch']._read_group(
            [('employee_id', 'in', self.ids)], ['employee_id'], ['__count']))
        for employee in self:
            employee.zk_punch_count = data.get(employee, 0)

    def _compute_zk_day_exception_count(self):
        data = dict(self.env['kairlyn.zk.attendance.day']._read_group(
            [('employee_id', 'in', self.ids), ('is_exception', '=', True)], ['employee_id'], ['__count']))
        for employee in self:
            employee.zk_day_exception_count = data.get(employee, 0)

    def write(self, vals):
        res = super().write(vals)
        if 'zk_attendance_required' in vals:
            self.env['kairlyn.zk.attendance.day']._queue_recompute(self)
        return res

    def action_view_zk_punches(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'kairlyn_zkteco_attendance.action_kairlyn_zk_punch')
        action['domain'] = [('employee_id', '=', self.id)]
        return action

    def action_view_zk_days(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'kairlyn_zkteco_attendance.action_kairlyn_zk_attendance_day')
        action['domain'] = [('employee_id', '=', self.id)]
        action['context'] = {'search_default_exception': 1}
        return action
