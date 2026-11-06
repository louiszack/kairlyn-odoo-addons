from datetime import datetime, time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

CONFIRM_WORD = '删除'


class KairlynZkPurgeWizard(models.TransientModel):
    """清理 Odoo 中的原始打卡记录，并可同时设为上线（例如上线前清空测试数据）。

    只删除 Odoo 里的数据，不会对设备做任何操作。
    """
    _name = 'kairlyn.zk.purge.wizard'
    _description = '清理 ZKTeco 原始打卡记录'

    device_ids = fields.Many2many('kairlyn.zk.device', string='设备', required=True)
    date_from = fields.Datetime('开始时间', help='留空表示不限。')
    date_to = fields.Datetime('结束时间', help='留空表示不限。')
    remove_unused_users = fields.Boolean(
        '同时删除无打卡记录的设备用户', default=False,
        help='删除后，员工映射也一起删除；设备上仍有这些用户的，下次同步会重新创建，但需要重新关联员工。'
             '上线时不要勾选。')
    go_live = fields.Boolean(
        '同时设为上线', default=True,
        help='把考勤机的导入起始时间、考勤计算起始日期、出勤记录生成起始日期都设为上线日期。'
             '上线日期之前的每日考勤和自动生成的出勤记录会被删除。')
    go_live_date = fields.Date('上线日期', default=fields.Date.context_today)
    set_import_start = fields.Boolean(
        '把设备的导入起始时间设为现在', default=True,
        help='防止设备上的旧记录在下次同步时被重新导入。')
    match_count = fields.Integer('将删除的记录数', compute='_compute_match_count')
    manual_attendance_count = fields.Integer(
        '上线前的手工出勤记录', compute='_compute_manual_attendance_count',
        help='上线日期之前、不是考勤机生成的出勤记录（例如测试时手工建的），不会自动删除。')
    confirm_text = fields.Char('确认', help='输入“%s”以确认。' % CONFIRM_WORD)

    def _domain(self):
        self.ensure_one()
        domain = [('device_id', 'in', self.device_ids.ids)]
        if self.date_from:
            domain.append(('punch_time', '>=', self.date_from))
        if self.date_to:
            domain.append(('punch_time', '<=', self.date_to))
        return domain

    def _manual_attendance_domain(self):
        self.ensure_one()
        employees = self.env['kairlyn.zk.device.user'].sudo().search(
            [('device_id', 'in', self.device_ids.ids)]).employee_id
        return [('employee_id', 'in', employees.ids), ('zk_day_id', '=', False),
                ('in_mode', '!=', 'technical'), ('date', '<', self.go_live_date)]

    @api.depends('device_ids', 'date_from', 'date_to')
    def _compute_match_count(self):
        for wizard in self:
            wizard.match_count = self.env['kairlyn.zk.punch'].search_count(wizard._domain()) \
                if wizard.device_ids else 0

    @api.depends('device_ids', 'go_live', 'go_live_date')
    def _compute_manual_attendance_count(self):
        for wizard in self:
            wizard.manual_attendance_count = self.env['hr.attendance'].sudo().search_count(
                wizard._manual_attendance_domain()) if wizard.device_ids and wizard.go_live and wizard.go_live_date else 0

    def action_view_manual_attendances(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('上线前的手工出勤记录'),
            'res_model': 'hr.attendance',
            'view_mode': 'list,form',
            'domain': self._manual_attendance_domain(),
            'target': 'current',
        }

    def action_purge(self):
        self.ensure_one()
        if not self.env.user.has_group('hr_attendance.group_hr_attendance_manager'):
            raise UserError(_('只有考勤管理员可以清理数据。'))
        if (self.confirm_text or '').strip() != CONFIRM_WORD:
            raise UserError(_('请在“确认”栏输入“%s”。', CONFIRM_WORD))
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise UserError(_('开始时间不能晚于结束时间。'))
        if self.go_live and not self.go_live_date:
            raise UserError(_('请填写上线日期。'))
        for device in self.device_ids:
            device._lock_for_button()  # 设备正在同步时不能清理

        Punch = self.env['kairlyn.zk.punch']
        per_device = dict(Punch._read_group(self._domain(), ['device_id'], ['__count']))
        punches = Punch.search(self._domain())
        count = len(punches)
        affected_employees = punches.employee_id
        punches.with_context(kairlyn_zk_purge=True).unlink()

        DeviceUser = self.env['kairlyn.zk.device.user'].sudo()
        removed_users = 0
        if self.remove_unused_users:
            used = set(self.env['kairlyn.zk.punch'].sudo().search(
                [('device_id', 'in', self.device_ids.ids)]).device_user_id.ids)
            unused = DeviceUser.search(
                [('device_id', 'in', self.device_ids.ids), ('id', 'not in', list(used))])
            removed_users = len(unused)
            unused.unlink()
        DeviceUser._refresh_stats(self.device_ids)

        now = fields.Datetime.now()
        if self.go_live:
            for device in self.device_ids:
                device.import_start = device._local_to_utc(
                    datetime.combine(self.go_live_date, time.min), device._get_zoneinfo())
            for company in self.device_ids.company_id.sudo():
                vals = {'zk_start_date': self.go_live_date, 'zk_attendance_start_date': self.go_live_date}
                if company.zk_lock_date and company.zk_lock_date < self.go_live_date:
                    vals['zk_lock_date'] = False  # 测试期间的锁定日期，上线后已无意义
                company.write(vals)  # 修改规则会自动重新计算
        elif self.set_import_start:
            self.device_ids.write({'import_start': now})
        if affected_employees:
            self.env['kairlyn.zk.attendance.day']._queue_recompute(affected_employees)

        if self.go_live:
            start_note = _('已设为上线日期 %s', self.go_live_date)
        else:
            start_note = _('已设为现在') if self.set_import_start else _('未修改')
        for device in self.device_ids:
            self.env['kairlyn.zk.sync.log'].sudo().create({
                'device_id': device.id,
                'trigger': 'purge',
                'state': 'done',
                'start_time': now,
                'end_time': now,
                'purged_count': per_device.get(device, 0),
                'message': _(
                    '清理范围：%(start)s 至 %(end)s（UTC）；所选设备共删除打卡记录 %(count)s 条、'
                    '设备用户 %(users)s 个；导入起始时间%(start_note)s。',
                    start=fields.Datetime.to_string(self.date_from) or _('不限'),
                    end=fields.Datetime.to_string(self.date_to) or _('不限'),
                    count=count, users=removed_users, start_note=start_note),
            })
        message = _('已删除 %s 条原始打卡记录。设备上的数据未受影响。', count)
        if self.go_live:
            message += _('已设为上线：从 %s 起计算每日考勤并生成出勤记录，后台正在重新计算。', self.go_live_date)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('清理完成'),
                'message': message,
                'type': 'success',
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
