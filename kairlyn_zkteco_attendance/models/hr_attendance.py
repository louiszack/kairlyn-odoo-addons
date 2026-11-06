from datetime import timedelta
import logging

from dateutil.relativedelta import relativedelta
import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import clean_context

_logger = logging.getLogger(__name__)

# 自动生成、更新出勤记录时使用的上下文标记；没有这个标记的修改视为人工操作
GENERATE_CONTEXT = 'kairlyn_zk_generate'
# 修改这些字段才算改动出勤时间
TIME_FIELDS = {'check_in', 'check_out', 'employee_id'}
MANUAL_REASONS = [
    ('forgot', '忘记打卡'),
    ('field', '外勤'),
    ('device', '设备故障'),
    ('other', '其他'),
]


def lock_error(day, lock_date, is_admin):
    """锁定日期及之前的出勤记录只有考勤管理员能改。允许时返回 None，否则返回原因。"""
    if lock_date and day <= lock_date and not is_admin:
        return _('考勤已锁定到 %s，这一天及之前的出勤记录只有考勤管理员可以修改。', lock_date)
    return None


def manual_rule_error(day, today, max_days, monthly_limit, used_days, reason, is_hr):
    """检查一次手工补录是否符合补录规则。允许时返回 None，否则返回原因。

    :param used_days: 本月已因“忘记打卡”补录过的天数（不含这一天）
    :param is_hr: 可以管理所有出勤记录的人（人事），不受期限和次数限制
    """
    if not reason:
        return _('请选择补卡原因。')
    if is_hr:
        return None
    if max_days and day < today - timedelta(days=max_days):
        return _('只能补录最近 %s 天的出勤记录，更早的请联系考勤管理员。', max_days)
    if reason == 'forgot' and monthly_limit and used_days >= monthly_limit:
        return _('该员工本月“忘记打卡”已补录 %s 天，达到上限，请联系考勤管理员。', used_days)
    return None


class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    in_mode = fields.Selection(selection_add=[('zkteco', 'ZKTeco 考勤机')], ondelete={'zkteco': 'set default'})
    out_mode = fields.Selection(selection_add=[('zkteco', 'ZKTeco 考勤机')], ondelete={'zkteco': 'set default'})
    zk_day_id = fields.Many2one(
        'kairlyn.zk.attendance.day', 'ZKTeco 每日考勤', readonly=True, copy=False,
        index='btree_not_null', ondelete='set null',
        help='由 ZKTeco 每日考勤自动生成时，指向对应的每日考勤结果。')
    zk_manual_reason = fields.Selection(
        MANUAL_REASONS, '补卡原因', tracking=True,
        help='手工新建或修改出勤记录（补录）时必须选择。每月“忘记打卡”的补录次数受考勤规则限制。')
    zk_manual_note = fields.Char('补卡说明', tracking=True)

    # ------------------------------------------------------------------
    # 补录规则与锁定
    # ------------------------------------------------------------------
    @api.model
    def _zk_is_backend(self, vals):
        """在后台手工操作，而不是员工自己在终端、系统托盘签到签退。"""
        modes = {vals.get('in_mode'), vals.get('out_mode')} - {None, False}
        return modes <= {'manual'}

    def _zk_workday(self):
        """所属工作日，与每日考勤一致（按员工时区和换日时间）。"""
        from .zk_attendance_day import workday_of
        self.ensure_one()
        employee = self.employee_id.sudo()
        tz = pytz.timezone(employee._get_tz() or 'UTC')
        day_change = timedelta(hours=employee.company_id.zk_day_change_hour or 0.0)
        return workday_of(pytz.utc.localize(self.check_in), tz, day_change)

    def _zk_targets(self):
        return [(att.employee_id, att._zk_workday())
                for att in self.sudo() if att.check_in and att.employee_id and att.in_mode != 'technical']

    def _zk_check_lock(self):
        is_admin = self.env.user.has_group('hr_attendance.group_hr_attendance_manager')
        for employee, day in self._zk_targets():
            error = lock_error(day, employee.company_id.zk_lock_date, is_admin)
            if error:
                raise UserError(error)

    def _zk_check_manual_rules(self):
        user = self.env.user
        is_admin = user.has_group('hr_attendance.group_hr_attendance_manager')
        is_hr = user.has_group('hr_attendance.group_hr_attendance_user')
        today = fields.Date.context_today(self)
        for att in self.sudo().filtered(lambda a: not a.zk_day_id and a.in_mode != 'technical'
                                        and a.check_in and a.employee_id):
            company = att.employee_id.company_id
            day = att._zk_workday()
            error = lock_error(day, company.zk_lock_date, is_admin)
            if not error:
                used = 0
                if att.zk_manual_reason == 'forgot' and company.zk_manual_monthly_limit and not is_hr:
                    used = att._zk_forgot_days_in_month(day)
                error = manual_rule_error(day, today, company.zk_manual_max_days,
                                          company.zk_manual_monthly_limit, used, att.zk_manual_reason, is_hr)
            if error:
                raise UserError(error)

    def _zk_forgot_days_in_month(self, day):
        """该员工本月（不含 day 这一天）已因“忘记打卡”补录过的天数。"""
        self.ensure_one()
        first = day.replace(day=1)
        last = first + relativedelta(months=1, days=-1)
        others = self.sudo().search([
            ('employee_id', '=', self.employee_id.id), ('id', '!=', self.id),
            ('zk_day_id', '=', False), ('zk_manual_reason', '=', 'forgot'), ('in_mode', '!=', 'technical'),
            ('date', '>=', first - timedelta(days=1)), ('date', '<=', last + timedelta(days=1)),
        ])
        return len({d for _employee, d in others._zk_targets() if first <= d <= last and d != day})

    @api.model
    def _zk_queue_recompute(self, targets):
        """手工补录变化后，尽快重新计算受影响员工从这一天起的每日考勤。"""
        earliest = {}
        for employee, day in targets:
            earliest[employee] = min(day, earliest.get(employee, day))
        Day = self.env['kairlyn.zk.attendance.day']
        for employee, day in earliest.items():
            Day._queue_recompute(employee, day - timedelta(days=1))

    @api.model
    def _zk_recompute_now(self, targets):
        """从每日考勤点“补录”保存后，立即重算这一天，关闭窗口就能看到结果。

        出错不影响补录本身：这一天已经在待重算队列里，后台稍后还会再算。
        """
        Day = self.env['kairlyn.zk.attendance.day'].with_context(clean_context(self.env.context))
        for employee, day in set(targets):
            try:
                with self.env.cr.savepoint():
                    Day._recompute_employee(employee, day, day)
            except Exception:
                _logger.exception('补录后立即重算 %s %s 的每日考勤失败，留给后台处理', employee.display_name, day)

    # ------------------------------------------------------------------
    # ORM
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get(GENERATE_CONTEXT):
            records.filtered(lambda a: a.in_mode == 'manual')._zk_check_manual_rules()
            targets = records._zk_targets()
            self._zk_queue_recompute(targets)
            if self.env.context.get('kairlyn_zk_recompute_now'):
                self._zk_recompute_now(targets)
        return records

    def write(self, vals):
        if self.env.context.get(GENERATE_CONTEXT):
            return super().write(vals)
        touches_time = bool(TIME_FIELDS & set(vals))
        if not touches_time and 'zk_manual_reason' not in vals:
            return super().write(vals)
        backend = self._zk_is_backend(vals)
        before = self._zk_targets() if touches_time else []
        if touches_time and backend:
            self._zk_check_lock()
        # 人工修改了自动生成的出勤记录：改为手工记录，以后不再自动覆盖
        days = self.zk_day_id if touches_time else self.env['kairlyn.zk.attendance.day']
        res = super().write(dict(vals, zk_day_id=False) if days else vals)
        if days:
            days.sudo().write({
                'attendance_status': 'manual',
                'attendance_message': _('自动生成的出勤记录已被 %s 手工修改，不再自动更新。', self.env.user.name),
            })
        if backend:
            self._zk_check_manual_rules()
        if touches_time:
            self._zk_queue_recompute(before + self._zk_targets())
        return res

    def unlink(self):
        if self.env.context.get(GENERATE_CONTEXT):
            return super().unlink()
        self._zk_check_lock()
        before = self._zk_targets()
        # 人工删除了自动生成的出勤记录：这一天不再自动生成，可在每日考勤里恢复
        days = self.zk_day_id
        res = super().unlink()
        if days:
            days.sudo().write({
                'attendance_status': 'deleted',
                'attendance_message': _('自动生成的出勤记录已被 %s 手工删除。', self.env.user.name),
            })
        self._zk_queue_recompute(before)
        return res
