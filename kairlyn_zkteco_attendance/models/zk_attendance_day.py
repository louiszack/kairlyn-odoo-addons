"""每日考勤结果：把原始打卡按员工的工作时间表整理成每人每天一条记录。

计划上下班时间、午休、公共假日和已批准的休假，全部取自 Odoo 员工（版本）上的工作时间表，
与 Odoo 出勤模块计算计划工时、扣除午休的口径一致。
本模型只做计算和展示，不写入出勤记录（hr.attendance）。
"""
from collections import defaultdict
from datetime import datetime, time, timedelta
import logging
import math

from psycopg2 import errors
import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command

from .hr_attendance import GENERATE_CONTEXT

_logger = logging.getLogger(__name__)

STATES = [
    ('normal', '正常'),
    ('late', '迟到'),
    ('early', '早退'),
    ('late_early', '迟到且早退'),
    ('missing_in', '缺签到'),
    ('missing_out', '缺签退'),
    ('absent', '缺勤'),
    ('rest_work', '休息日出勤'),
    ('ongoing', '进行中'),
]
EXCEPTION_STATES = {'late', 'early', 'late_early', 'missing_in', 'missing_out', 'absent'}
# 签到签退齐全、可以生成 Odoo 出勤记录的状态
GENERATE_STATES = {'normal', 'late', 'early', 'late_early', 'rest_work'}
ATTENDANCE_STATUSES = [
    ('disabled', '未启用'),
    ('waiting', '当天未结束'),
    ('generated', '已生成'),
    ('incomplete', '缺卡，待补录'),
    ('manual', '已手工补录'),
    ('review', '请假日有打卡，待审核'),
    ('ignored', '请假日打卡已忽略'),
    ('deleted', '已手工删除'),
    ('not_needed', '无需生成'),
    ('error', '生成失败'),
]
REVIEW_STATES = [
    ('pending', '待审核'),
    ('confirmed', '已确认生成'),
    ('ignored', '已忽略'),
]
WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
# 每次定时计算都会重算最近几天，覆盖较晚同步的打卡、当天状态和近期休假变动
RECENT_DAYS = 7


def _to_utc_naive(dt):
    return dt.astimezone(pytz.utc).replace(tzinfo=None)


def _minutes_over(seconds, grace_minutes):
    """超过容忍时间才计数；计数时从计划时间起算，不足一分钟按一分钟计。"""
    return math.ceil(seconds / 60) if seconds > grace_minutes * 60 else 0


# ----------------------------------------------------------------------
# 计算规则（纯函数：只接收带时区的时间，不访问数据库）
# ----------------------------------------------------------------------
def workday_of(moment, tz, day_change):
    """带时区的时间属于哪个工作日：换日时间之前算前一天。"""
    return (moment.astimezone(tz).replace(tzinfo=None) - day_change).date()


def split_punches(rows, tz, day_change, debounce):
    """按工作日分组，并去掉重复打卡。

    :param rows: [(打卡记录 id, 带时区的打卡时间)]，按时间排序
    :return: ({工作日: [全部打卡 id]}, {工作日: [有效打卡时间]})
    """
    raw_by_day = defaultdict(list)
    valid_by_day = defaultdict(list)
    last_time = last_day = None
    for punch_id, moment in rows:
        day = workday_of(moment, tz, day_change)
        raw_by_day[day].append(punch_id)
        if day == last_day and moment - last_time < debounce:
            continue
        last_time, last_day = moment, day
        valid_by_day[day].append(moment)
    return raw_by_day, valid_by_day


def subtract_intervals(spans, cuts, min_length=timedelta(0)):
    """从时段中扣除另一组时段（例如请假），并去掉短于 min_length 的碎片。"""
    result = []
    for start, stop in spans:
        pieces = [(start, stop)]
        for cut_start, cut_stop in cuts:
            remaining = []
            for piece_start, piece_stop in pieces:
                if cut_stop <= piece_start or cut_start >= piece_stop:
                    remaining.append((piece_start, piece_stop))
                    continue
                if cut_start > piece_start:
                    remaining.append((piece_start, cut_start))
                if cut_stop < piece_stop:
                    remaining.append((cut_stop, piece_stop))
            pieces = remaining
        result.extend(p for p in pieces if p[1] > p[0] and p[1] - p[0] >= min_length)
    return result


def overlap_seconds(spans, cuts):
    return sum(max((min(stop, cut_stop) - max(start, cut_start)).total_seconds(), 0)
               for start, stop in spans for cut_start, cut_stop in cuts)


def evaluate_day(valid, plan, lunches, flexible, required, is_today, late_grace, early_grace, noon,
                 leaves=(), explicit=False, intervals=None):
    """判定一天的考勤状态。

    :param valid: 当天去重后的有效打卡（带时区，按时间排序）；手工补录时为 [签到, 签退]
    :param plan: 计划 (上班, 下班)，带时区；弹性工时或非工作日为 None
    :param lunches: 当天的休息时段 [(开始, 结束)]
    :param noon: 只有一次打卡时（休息日、弹性工时），区分签到与签退的分界
    :param leaves: 当天本人已批准的请假时段：不计工时，也不生成出勤记录
    :param explicit: valid 来自手工补录的出勤记录，签到签退已经明确，不再按分界判断
    :param intervals: 手工补录的出勤时段，用来计算工时
    :return: 状态、签到、签退、迟到早退分钟数、工时，以及要生成出勤记录的时段 intervals
    """
    result = {'state': 'normal', 'check_in': None, 'check_out': None, 'late_minutes': 0,
              'early_minutes': 0, 'worked_hours': 0.0, 'rest_day': False, 'intervals': []}
    if not valid:
        result['state'] = 'absent'
        return result
    # 工作日：以计划时段的中点为分界，签到必须在分界之前、签退必须在分界之后
    # （相当于 ZKTime 的签到窗口、签退窗口）。休息日和弹性工时取第一次和最后一次打卡。
    middle = plan[0] + (plan[1] - plan[0]) / 2 if plan else noon
    morning_only = valid[-1] < middle
    afternoon_only = valid[0] >= middle
    if explicit:
        paired = len(valid) >= 2
    elif len(valid) == 1:
        paired = False
    else:
        paired = flexible or not plan or not (morning_only or afternoon_only)
    if not paired:
        if explicit or morning_only:
            result['check_in'], state = valid[0], 'missing_out'
        else:
            result['check_out'], state = valid[-1], 'missing_in'
    else:
        check_in, check_out = valid[0], valid[-1]
        result['check_in'], result['check_out'] = check_in, check_out
        if intervals is not None:
            spans = list(intervals)
        else:
            # 请假时段不算出勤；扣除后剩下的零碎时段（短于容忍时间）也不算
            spans = subtract_intervals([(check_in, check_out)], leaves,
                                       timedelta(minutes=max(late_grace, early_grace)))
        seconds = sum((stop - start).total_seconds() for start, stop in spans)
        if not flexible:
            seconds -= overlap_seconds(spans, lunches)
        result['worked_hours'] = max(seconds, 0) / 3600
        result['intervals'] = spans
        if flexible:
            state = 'normal'
        elif not plan:
            state, result['rest_day'] = 'rest_work', True
        elif not required:
            state = 'normal'
        else:
            late = _minutes_over((check_in - plan[0]).total_seconds(), late_grace)
            early = _minutes_over((plan[1] - check_out).total_seconds(), early_grace)
            result['late_minutes'], result['early_minutes'] = late, early
            state = 'late_early' if late and early else 'late' if late else 'early' if early else 'normal'
    if is_today:
        # 当天尚未结束：签退和早退都还不确定
        state = 'ongoing'
        result['early_minutes'] = 0
    result['state'] = state
    return result


def has_leave_conflict(valid, leaves, grace, full_day_leave):
    """本人已批准的请假时段内有打卡，需要审核。

    :param full_day_leave: 这一天的计划工时全部请假了；这时有任何打卡都算
    :param grace: 请假开始后、结束前这么长时间内的打卡不算（例如提前一点回来）
    """
    if not valid or not leaves:
        return False
    if full_day_leave:
        return True
    return any(start + grace < moment < stop - grace for moment in valid for start, stop in leaves)


def judge_absence(state, late_minutes, early_minutes, planned_hours, hours_per_day,
                  half_minutes, full_minutes):
    """按旷工规则折算旷工天数（0、0.5 或 1）。

    工作日没有打卡、没有请假也没有补录记 1 天，只有半天班（另半天请假）时记 0.5 天；
    迟到、早退分别判断，超过 full_minutes 记 1 天，超过 half_minutes 记 0.5 天，同一天取重的。
    分钟数为 0 表示不启用该档。
    """
    if state == 'absent':
        return 0.5 if hours_per_day and planned_hours <= hours_per_day / 2 + 0.01 else 1.0
    if state not in ('late', 'early', 'late_early'):
        return 0.0

    def level(minutes):
        if full_minutes and minutes > full_minutes:
            return 1.0
        if half_minutes and minutes > half_minutes:
            return 0.5
        return 0.0

    return max(level(late_minutes), level(early_minutes))


class KairlynZkAttendanceDay(models.Model):
    _name = 'kairlyn.zk.attendance.day'
    _description = 'ZKTeco 每日考勤结果'
    _order = 'date desc, employee_id'

    employee_id = fields.Many2one('hr.employee', '员工', required=True, index=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', '公司', required=True, index=True)
    department_id = fields.Many2one(related='employee_id.department_id', store=True, string='部门')
    date = fields.Date('日期', required=True, index=True)
    weekday = fields.Char('星期', compute='_compute_weekday')
    calendar_id = fields.Many2one('resource.calendar', '工作时间表')
    is_flexible = fields.Boolean('弹性工时')
    attendance_required = fields.Boolean('参与考勤')

    planned_start = fields.Datetime('计划上班')
    planned_end = fields.Datetime('计划下班')
    planned_hours = fields.Float('计划工时')
    check_in = fields.Datetime('签到')
    check_out = fields.Datetime('签退')
    punch_count = fields.Integer('有效打卡次数', help='去掉重复打卡后的次数。')
    late_minutes = fields.Integer('迟到（分钟）')
    early_minutes = fields.Integer('早退（分钟）')
    worked_hours = fields.Float(
        '出勤工时', help='签到到签退的时长，扣除本人请假时段；固定工时按工作时间表扣除休息时段。')
    absent_days = fields.Float(
        '旷工（天）', digits=(16, 1),
        help='按“旷工规则”折算：工作日没有打卡、没有请假也没有补录记 1 天（只有半天班时记 0.5 天）；'
             '迟到或早退超过设定的分钟数记 0.5 天或 1 天。')

    state = fields.Selection(STATES, '状态', required=True, index=True)
    is_exception = fields.Boolean('异常', index=True)
    note = fields.Char('说明')
    punch_ids = fields.Many2many(
        'kairlyn.zk.punch', 'kairlyn_zk_attendance_day_punch_rel', 'day_id', 'punch_id', '当天打卡')
    computed_at = fields.Datetime('计算时间')
    locked = fields.Boolean('已锁定', compute='_compute_locked')

    attendance_ids = fields.One2many('hr.attendance', 'zk_day_id', '生成的出勤记录')
    attendance_id = fields.Many2one('hr.attendance', '出勤记录', compute='_compute_attendance_id')
    attendance_status = fields.Selection(
        ATTENDANCE_STATUSES, '出勤记录状态', default='disabled', required=True, index=True)
    attendance_message = fields.Char('出勤记录说明')

    leave_conflict = fields.Boolean(
        '请假日有打卡', help='本人已批准的请假时段内有打卡。审核前不生成出勤记录。')
    review_state = fields.Selection(REVIEW_STATES, '审核结果')
    reviewed_by = fields.Many2one('res.users', '审核人')
    reviewed_at = fields.Datetime('审核时间')

    _employee_date_uniq = models.Constraint(
        'UNIQUE(employee_id, date)',
        '同一员工同一天只能有一条考勤结果。',
    )

    @api.depends('date')
    def _compute_weekday(self):
        for rec in self:
            rec.weekday = WEEKDAYS[rec.date.weekday()] if rec.date else ''

    @api.depends('date', 'company_id.zk_lock_date')
    def _compute_locked(self):
        for rec in self:
            lock = rec.company_id.zk_lock_date
            rec.locked = bool(lock and rec.date and rec.date <= lock)

    @api.depends('attendance_ids')
    def _compute_attendance_id(self):
        for rec in self:
            rec.attendance_id = rec.attendance_ids[:1]

    def action_open_manual_attendance(self):
        """缺卡、缺勤的日子：打开 Odoo 出勤表单补录，员工和已打到的时间自动填好。"""
        self.ensure_one()
        if self.locked:
            raise UserError(_('%s 已锁定，不能补录。', self.display_name))
        context = {
            'default_employee_id': self.employee_id.id,
            'kairlyn_zk_recompute_now': True,  # 保存后立即重算这一天
        }
        # 已打到的一侧用真实打卡时间，缺的一侧先填计划时间，由补录的人改成实际时间
        check_in = self.check_in or self.planned_start
        check_out = self.check_out or self.planned_end
        if check_in:
            context['default_check_in'] = fields.Datetime.to_string(check_in)
        if check_out:
            context['default_check_out'] = fields.Datetime.to_string(check_out)
        return {
            'type': 'ir.actions.act_window',
            'name': _('补录出勤：%s', self.display_name),
            'res_model': 'hr.attendance',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': context,
        }

    def action_regenerate_attendance(self):
        """恢复自动生成：用于人工删除过、或生成失败的日子。"""
        if not self.env.user.has_group('hr_attendance.group_hr_attendance_manager'):
            raise UserError(_('只有考勤管理员可以重新生成出勤记录。'))
        for day in self:
            day.sudo().write({'attendance_status': 'disabled', 'attendance_message': False})
            self._recompute_employee(day.employee_id, day.date, day.date)

    # ------------------------------------------------------------------
    # 请假日有打卡：审核
    # ------------------------------------------------------------------
    def _check_reviewer(self):
        if self.env.user.has_group('hr_attendance.group_hr_attendance_user'):
            return
        for day in self:
            if day.employee_id.sudo().attendance_manager_id != self.env.user:
                raise UserError(_('只有 %s 的出勤审批人或考勤管理员可以审核。', day.employee_id.name))

    def _set_review(self, state):
        days = self.filtered('leave_conflict')
        days._check_reviewer()
        locked = days.filtered('locked')
        if locked:
            raise UserError(_('%s 已锁定，不能再审核。', locked[0].display_name))
        decided = state != 'pending'
        days.sudo().write({
            'review_state': state,
            'reviewed_by': self.env.uid if decided else False,
            'reviewed_at': fields.Datetime.now() if decided else False,
        })
        for day in days:
            self._recompute_employee(day.employee_id, day.date, day.date)
        return True

    def action_review_confirm(self):
        """员工请假照休、另外回来工作：按实际打卡生成出勤记录（Odoo 会按加班规则计算）。"""
        return self._set_review('confirmed')

    def action_review_ignore(self):
        """误打卡或只是回来拿东西：不生成出勤记录。"""
        return self._set_review('ignored')

    def action_review_reset(self):
        return self._set_review('pending')

    @api.depends('employee_id', 'date')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '%s %s' % (rec.employee_id.name or '', fields.Date.to_string(rec.date) or '')

    # ------------------------------------------------------------------
    # 计算队列
    # ------------------------------------------------------------------
    @api.model
    def _get_target_employees(self, companies=None):
        """参与计算的员工：在任意考勤机上有映射的员工（含已归档的员工）。"""
        employees = self.env['kairlyn.zk.device.user'].sudo().search(
            [('employee_id', '!=', False)]).employee_id
        if companies is not None:
            employees = employees.filtered(lambda e: e.company_id in companies)
        return employees

    @api.model
    def _queue_recompute(self, employees, date_from=None, trigger=True):
        """登记需要重新计算的员工，由定时任务在后台处理。

        只新增待重算请求，不修改员工档案，避免与正在计算的任务同时修改同一行数据。

        :param date_from: 从哪天起重新计算；留空表示从公司设置的起始日期起。
        :param trigger: 是否立即触发计算任务；同步分批入库时最后统一触发。
        """
        vals = []
        for employee in employees.sudo().with_context(active_test=False):
            start = date_from or employee.company_id.zk_start_date
            if start:
                vals.append({'employee_id': employee.id, 'date_from': fields.Date.to_date(start)})
        if vals:
            self.env['kairlyn.zk.recompute.request'].sudo().create(vals)
            if trigger:
                self._trigger_compute()
        return bool(vals)

    @api.model
    def _trigger_compute(self):
        self.env.ref('kairlyn_zkteco_attendance.ir_cron_kairlyn_zk_compute_days').sudo()._trigger()

    @api.model
    def _cron_compute_days(self):
        Request = self.env['kairlyn.zk.recompute.request'].sudo()
        requests_by_employee = Request.search([]).grouped('employee_id')
        for employee, requests in requests_by_employee.items():
            if self._safe_recompute(employee, min(requests.mapped('date_from'))):
                requests.unlink()  # 计算期间新增的请求不在其中，下次处理
            self.env['ir.cron']._commit_progress(1)

        recent = fields.Date.today() - timedelta(days=RECENT_DAYS)
        for employee in self._get_target_employees():
            self._safe_recompute(employee, recent)
            self.env['ir.cron']._commit_progress(1)

    def _safe_recompute(self, employee, date_from):
        """计算一名员工；出错不影响其他员工。

        :return: 是否可以删除对应的待重算请求。遇到数据库并发冲突时返回 False，下次再算。
        """
        try:
            with self.env.cr.savepoint():
                self._recompute_employee(employee, date_from)
        except (errors.SerializationFailure, errors.DeadlockDetected):
            _logger.info('计算员工 %s 的每日考勤时遇到数据库并发冲突，下次再算', employee.display_name)
            return False
        except Exception as e:
            _logger.exception('计算员工 %s 的每日考勤失败', employee.display_name)
            employee.message_post(body=_('ZKTeco 每日考勤计算失败：%s', e))
        return True

    # ------------------------------------------------------------------
    # 计算
    # ------------------------------------------------------------------
    @api.model
    def _recompute_employee(self, employee, date_from, date_to=None):
        """重新计算一名员工在 [date_from, date_to] 期间（含首尾）的每日考勤结果。

        返回计算出的天数。早于公司起始日期、或在锁定日期及之前的日子不计算。
        """
        employee = employee.sudo().with_context(active_test=False)
        company = employee.company_id
        if not company.zk_start_date:
            return 0

        tz = pytz.timezone(employee._get_tz() or 'UTC')
        day_change = timedelta(hours=company.zk_day_change_hour or 0.0)
        now = fields.Datetime.now()
        now_local = pytz.utc.localize(now).astimezone(tz)
        today = (now_local.replace(tzinfo=None) - day_change).date()  # 当前所在的工作日

        # 起始日期之前的结果已不在计算范围内（例如上线前清空测试数据后调整了起始日期），删除未锁定的部分
        outdated = [('employee_id', '=', employee.id), ('date', '<', company.zk_start_date)]
        if company.zk_lock_date:
            outdated.append(('date', '>', company.zk_lock_date))
        self._delete_days(self.sudo().search(outdated))

        start = max(fields.Date.to_date(date_from), company.zk_start_date)
        if company.zk_lock_date:
            start = max(start, company.zk_lock_date + timedelta(days=1))
        end = min(fields.Date.to_date(date_to) if date_to else today, today)
        if start > end:
            return 0

        def localize(naive):
            return tz.localize(naive, is_dst=False)

        range_start = localize(datetime.combine(start, time.min) + day_change)
        range_end = localize(datetime.combine(end + timedelta(days=1), time.min) + day_change)

        # 1. 打卡：按工作日分组，并去掉重复打卡
        punches = self.env['kairlyn.zk.punch'].sudo().search([
            ('employee_id', '=', employee.id),
            ('punch_time', '>=', _to_utc_naive(range_start)),
            ('punch_time', '<', _to_utc_naive(range_end)),
        ], order='punch_time, id')
        raw_by_day, valid_by_day = split_punches(
            [(punch.id, pytz.utc.localize(punch.punch_time)) for punch in punches],
            tz, day_change, timedelta(minutes=company.zk_debounce_minutes or 0))

        # 2. 计划：Odoo 按员工版本上的工作时间表算出的工作时段（已扣除公共假日和休假）
        plan_by_day = {}
        lunch_by_day = defaultdict(list)
        plan_note = ''
        if not employee.tz:
            plan_note = _('员工未设置时区，无法取得计划上下班时间')
        else:
            try:
                intervals = employee._employee_attendance_intervals(range_start, range_end)
                lunches = employee._employee_attendance_intervals(range_start, range_end, lunch=True)
            except Exception as e:  # 工作时间表配置不完整等情况
                _logger.warning('无法取得员工 %s 的计划工作时段：%s', employee.display_name, e)
                intervals = lunches = []
                plan_note = _('无法取得计划工作时段，请检查员工的工作时间表')
            for begin, stop, _meta in intervals:
                day = workday_of(begin, tz, day_change)
                plan = plan_by_day.setdefault(day, [begin, stop, 0.0])
                plan[0] = min(plan[0], begin)
                plan[1] = max(plan[1], stop)
                plan[2] += (stop - begin).total_seconds() / 3600
            for begin, stop, _meta in lunches:
                lunch_by_day[workday_of(begin, tz, day_change)].append((begin, stop))

        # 3. 本人已批准的请假时段（公共假日已经体现在计划里）
        leave_by_day = defaultdict(list)
        if employee.resource_id:
            leaves = self.env['resource.calendar.leaves'].sudo().search([
                ('resource_id', '=', employee.resource_id.id),
                ('time_type', '=', 'leave'),
                ('date_from', '<', _to_utc_naive(range_end)),
                ('date_to', '>', _to_utc_naive(range_start)),
            ])
            for leave in leaves:
                leave_start = pytz.utc.localize(leave.date_from)
                leave_stop = pytz.utc.localize(leave.date_to)
                day = workday_of(max(leave_start, range_start), tz, day_change)
                while day <= end:
                    window_start = localize(datetime.combine(day, time.min) + day_change)
                    if window_start >= leave_stop:
                        break
                    window_stop = localize(datetime.combine(day + timedelta(days=1), time.min) + day_change)
                    clip_start, clip_stop = max(leave_start, window_start), min(leave_stop, window_stop)
                    if clip_start < clip_stop:
                        leave_by_day[day].append((clip_start, clip_stop))
                    day += timedelta(days=1)

        # 4. 手工补录的出勤记录（不是本模块生成的，也不含 Odoo 缺勤管理建的技术性记录）
        manual_by_day = defaultdict(list)
        manual_attendances = self.env['hr.attendance'].sudo().search([
            ('employee_id', '=', employee.id),
            ('zk_day_id', '=', False),
            ('in_mode', '!=', 'technical'),
            ('check_in', '>=', _to_utc_naive(range_start)),
            ('check_in', '<', _to_utc_naive(range_end)),
        ], order='check_in')
        for attendance in manual_attendances:
            check_in = pytz.utc.localize(attendance.check_in)
            check_out = pytz.utc.localize(attendance.check_out) if attendance.check_out else None
            manual_by_day[workday_of(check_in, tz, day_change)].append((check_in, check_out))

        # 5. 是否可以判定缺勤
        departure = employee.departure_date
        contract_starts = employee.version_ids.filtered('contract_date_start').mapped('contract_date_start')
        first_contract = min(contract_starts) if contract_starts else None

        # 没有关联任何考勤机用户的员工不会有打卡，不判缺勤
        has_device_user = bool(employee.zk_device_user_ids)

        def absence_allowed(day):
            if not has_device_user or not employee.zk_attendance_required or day >= today:
                return False
            if not employee.active and not departure:
                return False
            if departure and day > departure:
                return False
            return not (first_contract and day < first_contract)

        days = {d for d in valid_by_day if start <= d <= end}
        days |= {d for d in manual_by_day if start <= d <= end}
        days |= {d for d in plan_by_day if start <= d <= end and absence_allowed(d)}

        late_grace = company.zk_late_grace
        early_grace = company.zk_early_grace
        grace = timedelta(minutes=max(late_grace, early_grace))
        existing = self.sudo().search([
            ('employee_id', '=', employee.id), ('date', '>=', start), ('date', '<=', end)])
        by_date = {rec.date: rec for rec in existing}
        vals_by_day = {}
        intervals_by_day = {}
        for day in sorted(days):
            calendar = employee._get_calendars(day).get(employee.id) or employee.resource_calendar_id
            flexible = bool(calendar.flexible_hours)
            required = employee.zk_attendance_required
            plan = None if flexible else plan_by_day.get(day)
            valid = valid_by_day.get(day, [])
            manual = manual_by_day.get(day)
            day_leaves = leave_by_day.get(day, [])
            if not valid and not plan and not manual:
                continue  # 弹性工时只按打卡记录，不判缺勤
            args = ((plan[0], plan[1]) if plan else None, lunch_by_day.get(day, []), flexible, required,
                    day >= today, late_grace, early_grace, localize(datetime.combine(day, time(12))))
            notes = [plan_note] if plan_note else []
            conflict = False
            if manual:
                # 有手工补录：以补录的签到签退为准，照常判迟到、早退
                check_outs = [m[1] for m in manual]
                points = [manual[0][0]] + ([max(check_outs)] if all(check_outs) else [])
                result = evaluate_day(points, *args, explicit=True, intervals=[m for m in manual if m[1]])
                notes.append(_('按手工补录的出勤记录计算'))
            else:
                result = evaluate_day(valid, *args, leaves=day_leaves)
                conflict = required and has_leave_conflict(
                    valid, day_leaves, grace, full_day_leave=bool(day_leaves) and not flexible and not plan)
                if day_leaves and not conflict and result['check_in']:
                    notes.append(_('已扣除请假时段'))
            if flexible:
                notes.append(_('弹性工时，不计迟到、早退'))
            if not required:
                notes.append(_('不参与考勤，不标记异常'))
            if result['rest_day']:
                notes.append(_('非工作日（休息日、公共假日或请假）'))

            rec = by_date.get(day)
            if conflict:
                review = {'review_state': (rec.review_state if rec else False) or 'pending'}
                notes.append(_('请假时段内有打卡：员工实际来上班的请取消或修改请假；'
                               '请假照休、另外回来工作的点“确认生成”；误打卡的点“忽略”'))
            else:
                review = {'review_state': False, 'reviewed_by': False, 'reviewed_at': False}
            pending = conflict and review['review_state'] == 'pending'
            absent_days = judge_absence(
                result['state'], result['late_minutes'], result['early_minutes'],
                plan[2] if plan else 0.0, calendar.hours_per_day,
                company.zk_absent_half_minutes, company.zk_absent_full_minutes) if required else 0.0

            if conflict and review['review_state'] == 'confirmed':
                # 确认按实际打卡生成：请假时段不扣除
                spans = [(result['check_in'], result['check_out'])] \
                    if result['check_in'] and result['check_out'] else []
            else:
                spans = result['intervals']
            intervals_by_day[day] = [(_to_utc_naive(a), _to_utc_naive(b)) for a, b in spans]

            vals = {
                'employee_id': employee.id,
                'company_id': company.id,
                'date': day,
                'calendar_id': calendar.id or False,
                'is_flexible': flexible,
                'attendance_required': required,
                'planned_start': _to_utc_naive(plan[0]) if plan else False,
                'planned_end': _to_utc_naive(plan[1]) if plan else False,
                'planned_hours': plan[2] if plan else (calendar.hours_per_day if flexible else 0.0),
                'check_in': _to_utc_naive(result['check_in']) if result['check_in'] else False,
                'check_out': _to_utc_naive(result['check_out']) if result['check_out'] else False,
                'punch_count': len(valid),
                'late_minutes': result['late_minutes'],
                'early_minutes': result['early_minutes'],
                'worked_hours': result['worked_hours'],
                'absent_days': absent_days,
                'state': result['state'],
                'is_exception': required and (result['state'] in EXCEPTION_STATES or absent_days > 0 or pending),
                'leave_conflict': conflict,
                **review,
                'note': '；'.join(notes),
                'punch_ids': [Command.set(raw_by_day.get(day, []))],
                'computed_at': now,
            }
            vals_by_day[day] = vals

        # 6. 写入：更新已有记录，新增缺少的，删除范围内已不需要的
        to_create = []
        for day, vals in vals_by_day.items():
            rec = by_date.pop(day, None)
            if rec:
                rec.write(vals)
            else:
                to_create.append(vals)
        if to_create:
            self.sudo().create(to_create)
        if by_date:
            self._delete_days(self.sudo().browse([rec.id for rec in by_date.values()]))

        # 7. 根据每日考勤生成、更新或删除 Odoo 出勤记录
        days = self.sudo().search([
            ('employee_id', '=', employee.id), ('date', '>=', start), ('date', '<=', end)])
        self._sync_attendances(employee, days, company.zk_attendance_start_date, intervals_by_day)
        return len(vals_by_day)

    @api.model
    def _delete_days(self, days):
        """删除每日考勤结果，连同本模块为它们生成的出勤记录。"""
        days.attendance_ids.sudo().with_context(**{GENERATE_CONTEXT: True}).unlink()
        days.unlink()

    @api.model
    def _sync_attendances(self, employee, days, generate_from, intervals_by_day=None):
        """让 Odoo 出勤记录与每日考勤保持一致。

        - 签到签退齐全、参与考勤的日子：生成或更新出勤记录（使用真实打卡时间）；
          本人请假的时段不算出勤，有部分时段请假时会拆成请假前、请假后两条；
        - 缺卡、缺勤、当天未结束：不生成；以前生成过的删除；
        - 当天已有手工补录的出勤记录：以手工记录为准，不再生成；
        - 请假时段内有打卡：审核前不生成；
        - 人工删除过的日子：不再生成，直到在每日考勤里点击“重新生成出勤记录”。
        """
        intervals_by_day = intervals_by_day or {}

        def generator(records):
            return records.sudo().with_context(**{GENERATE_CONTEXT: True})

        Attendance = generator(self.env['hr.attendance'])
        for day in days:
            own = generator(day.attendance_ids).sorted('check_in')

            def set_status(status, message=False, day=day):
                if day.attendance_status != status or day.attendance_message != message:
                    day.write({'attendance_status': status, 'attendance_message': message})

            if not generate_from or day.date < generate_from:
                # 未启用或早于生成起始日期：不生成，已生成的保留不动
                if not own:
                    set_status('disabled')
                continue
            if day.attendance_status == 'deleted' and not own:
                continue

            # 手工补录（Odoo 缺勤管理自动建的技术性记录不算）
            same_day = [('date', '=', day.date)]
            if day.check_in and day.check_out:
                same_day = ['|', ('date', '=', day.date),
                            '&', ('check_in', '<', day.check_out),
                            '|', ('check_out', '=', False), ('check_out', '>', day.check_in)]
            others = Attendance.search([
                ('employee_id', '=', employee.id), ('id', 'not in', own.ids), ('in_mode', '!=', 'technical'),
            ] + same_day)
            if others.filtered(lambda a: not a.zk_day_id):
                own.unlink()
                set_status('manual')
                continue

            if day.leave_conflict and day.review_state != 'confirmed':
                own.unlink()
                set_status('ignored' if day.review_state == 'ignored' else 'review')
                continue

            desired = intervals_by_day.get(day.date)
            if desired is None:
                desired = [(day.check_in, day.check_out)] if day.check_in and day.check_out else []
            if not (day.attendance_required and day.state in GENERATE_STATES and desired):
                own.unlink()
                set_status('waiting' if day.state == 'ongoing' else
                           'incomplete' if day.state in ('missing_in', 'missing_out') else 'not_needed')
                continue

            if [(a.check_in, a.check_out) for a in own] != desired:
                try:
                    with self.env.cr.savepoint():
                        if len(own) == len(desired) == 1:
                            own.write({'check_in': desired[0][0], 'check_out': desired[0][1]})
                        else:
                            own.unlink()
                            Attendance.create([{
                                'employee_id': employee.id,
                                'check_in': check_in,
                                'check_out': check_out,
                                'in_mode': 'zkteco',
                                'out_mode': 'zkteco',
                                'zk_day_id': day.id,
                            } for check_in, check_out in desired])
                except Exception as e:  # 例如与其他出勤记录时间重叠
                    message = str(e)[:500]
                    if day.attendance_status != 'error' or day.attendance_message != message:
                        _logger.warning('为 %s 生成出勤记录失败：%s', day.display_name, e)
                    set_status('error', message)
                    continue
            set_status('generated')
