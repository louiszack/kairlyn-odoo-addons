from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# 修改后需要重新计算每日考勤结果的规则字段
RULE_FIELDS = (
    'zk_start_date', 'zk_lock_date', 'zk_late_grace', 'zk_early_grace',
    'zk_debounce_minutes', 'zk_day_change_hour', 'zk_attendance_start_date',
    'zk_absent_half_minutes', 'zk_absent_full_minutes',
)


class ResCompany(models.Model):
    _inherit = 'res.company'

    zk_start_date = fields.Date(
        '考勤计算起始日期', help='从这一天开始计算每日考勤结果。留空则不计算。')
    zk_lock_date = fields.Date(
        '考勤锁定日期',
        help='这一天及之前：每日考勤不再重新计算；出勤记录（包括手工补录）只有考勤管理员可以新建、修改、删除。'
             '加班审批不受影响。')
    zk_late_grace = fields.Integer(
        '迟到容忍（分钟）', default=15,
        help='签到晚于计划上班时间超过此分钟数才记迟到，迟到分钟数从计划上班时间起算。')
    zk_early_grace = fields.Integer(
        '早退容忍（分钟）', default=15,
        help='签退早于计划下班时间超过此分钟数才记早退，早退分钟数从计划下班时间起算。')
    zk_debounce_minutes = fields.Integer(
        '重复打卡间隔（分钟）', default=5,
        help='与上一次有效打卡间隔小于此值的打卡视为重复，只保留第一次。')
    zk_day_change_hour = fields.Float(
        '换日时间', default=4.0,
        help='此时间之前的打卡计入前一个工作日。')
    zk_attendance_start_date = fields.Date(
        '出勤记录生成起始日期',
        help='从这一天起，把签到签退齐全的每日考勤写入 Odoo 出勤记录；'
             '仪表盘、出勤报表、加班和按出勤计薪都以出勤记录为准。留空则只计算每日考勤，不生成出勤记录。')
    zk_absent_half_minutes = fields.Integer(
        '旷工半天（分钟）', default=30,
        help='迟到或早退超过此分钟数，记旷工半天。0 表示不启用。')
    zk_absent_full_minutes = fields.Integer(
        '旷工一天（分钟）', default=240,
        help='迟到或早退超过此分钟数，记旷工一天。0 表示不启用。')
    zk_manual_monthly_limit = fields.Integer(
        '每月忘记打卡补录次数', default=3,
        help='每人每月因“忘记打卡”最多补录几天。0 表示不限。可以管理所有出勤记录的人事不受限制。')
    zk_manual_max_days = fields.Integer(
        '补录期限（天）', default=7,
        help='只能补录最近几天的出勤记录。0 表示不限。可以管理所有出勤记录的人事不受限制。')
    zk_log_keep_days = fields.Integer(
        '同步日志保留天数', default=90,
        help='超过此天数的同步、校准时间日志会自动删除；清理数据的日志永久保留。')

    @api.constrains('zk_late_grace', 'zk_early_grace', 'zk_debounce_minutes', 'zk_day_change_hour')
    def _check_zk_rules(self):
        for company in self:
            if min(company.zk_late_grace, company.zk_early_grace, company.zk_debounce_minutes) < 0:
                raise ValidationError(_('容忍时间和重复打卡间隔不能为负数。'))
            if not 0 <= company.zk_day_change_hour < 24:
                raise ValidationError(_('换日时间必须在 00:00 到 23:59 之间。'))

    @api.constrains('zk_absent_half_minutes', 'zk_absent_full_minutes', 'zk_manual_monthly_limit',
                    'zk_manual_max_days', 'zk_log_keep_days')
    def _check_zk_absence_rules(self):
        for company in self:
            if min(company.zk_absent_half_minutes, company.zk_absent_full_minutes,
                   company.zk_manual_monthly_limit, company.zk_manual_max_days) < 0:
                raise ValidationError(_('旷工分钟数、补录次数和补录期限不能为负数。'))
            if company.zk_absent_half_minutes and company.zk_absent_full_minutes \
                    and company.zk_absent_full_minutes <= company.zk_absent_half_minutes:
                raise ValidationError(_('记旷工一天的分钟数必须大于记旷工半天的分钟数。'))
            if company.zk_log_keep_days < 7:
                raise ValidationError(_('同步日志至少保留 7 天。'))

    @api.constrains('zk_attendance_start_date', 'zk_start_date')
    def _check_zk_attendance_start(self):
        for company in self.filtered('zk_attendance_start_date'):
            if not company.zk_start_date:
                raise ValidationError(_('请先设置“考勤计算起始日期”，再设置出勤记录生成起始日期。'))
            if company.zk_attendance_start_date < company.zk_start_date:
                raise ValidationError(_('出勤记录生成起始日期不能早于考勤计算起始日期，因为出勤记录是根据每日考勤生成的。'))

    def write(self, vals):
        keys = [key for key in RULE_FIELDS if key in vals]
        before = {company.id: [company[key] for key in keys] for company in self} if keys else {}
        res = super().write(vals)
        if keys:
            # 设置页保存时会连同未修改的字段一起写入，只有值真正变化才重新计算
            changed = self.filtered(lambda c: [c[key] for key in keys] != before[c.id])
            if changed:
                Day = self.env['kairlyn.zk.attendance.day']
                Day._queue_recompute(Day._get_target_employees(changed))
        return res
