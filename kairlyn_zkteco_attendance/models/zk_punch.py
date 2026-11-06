from odoo import _, api, fields, models
from odoo.exceptions import UserError

# 设备上报的验证方式代码；未列出的按代码显示
VERIFY_TYPE_LABELS = {
    0: '密码',
    1: '指纹',
    2: '刷卡',
    15: '人脸',
}


class KairlynZkPunch(models.Model):
    """从设备读取的原始打卡记录。只读，不做任何业务判断。"""
    _name = 'kairlyn.zk.punch'
    _description = 'ZKTeco 原始打卡记录'
    _order = 'punch_time desc, id desc'

    device_id = fields.Many2one(
        'kairlyn.zk.device', '设备', required=True, readonly=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(related='device_id.company_id', store=True)
    device_user_id = fields.Many2one(
        'kairlyn.zk.device.user', '设备用户', required=True, readonly=True,
        ondelete='restrict', index=True)
    user_code = fields.Char('设备用户编号', required=True, readonly=True)
    employee_id = fields.Many2one(
        related='device_user_id.employee_id', store=True, index=True, string='员工')
    punch_time = fields.Datetime('打卡时间', required=True, readonly=True, index=True)
    device_local_time = fields.Char(
        '设备本地时间', readonly=True, help='设备记录的原始时间（未做时区换算），供核对。')
    verify_type = fields.Integer('验证方式代码', readonly=True)
    verify_type_label = fields.Char('验证方式', compute='_compute_verify_type_label')
    punch_state = fields.Integer('打卡状态代码', readonly=True,
                                 help='设备上的状态键（上班/下班等）。设备未启用状态键时恒为 0。')
    device_uid = fields.Integer('设备内部序号', readonly=True)
    sync_log_id = fields.Many2one('kairlyn.zk.sync.log', '同步批次', readonly=True, ondelete='set null')

    _device_user_time_uniq = models.Constraint(
        'UNIQUE(device_id, user_code, punch_time)',
        '该打卡记录已存在。',
    )

    @api.depends('verify_type')
    def _compute_verify_type_label(self):
        for rec in self:
            rec.verify_type_label = VERIFY_TYPE_LABELS.get(rec.verify_type) or _('代码 %s', rec.verify_type)

    @api.depends('user_code', 'punch_time', 'employee_id')
    def _compute_display_name(self):
        for rec in self:
            who = rec.employee_id.name or rec.user_code or ''
            rec.display_name = '%s %s' % (who, fields.Datetime.to_string(rec.punch_time) or '')

    def write(self, vals):
        # 只允许映射变更引起的关联字段重算，禁止修改原始数据
        protected = set(vals) - {'employee_id', 'sync_log_id'}
        if protected and not self.env.context.get('kairlyn_zk_allow_write'):
            raise UserError(_('原始打卡记录不允许修改。'))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_via_purge(self):
        if not self.env.context.get('kairlyn_zk_purge'):
            raise UserError(_('原始打卡记录不允许直接删除，请使用“清理测试数据”功能。'))
