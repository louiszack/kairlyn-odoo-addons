from datetime import timedelta

from odoo import api, fields, models


class KairlynZkSyncLog(models.Model):
    _name = 'kairlyn.zk.sync.log'
    _description = 'ZKTeco 同步日志'
    _order = 'id desc'
    _rec_name = 'start_time'

    device_id = fields.Many2one('kairlyn.zk.device', '设备', ondelete='cascade', index=True, readonly=True)
    company_id = fields.Many2one(related='device_id.company_id', store=True)
    trigger = fields.Selection(
        [('cron', '定时同步'), ('manual', '手动同步'), ('time', '校准时间'), ('purge', '清理数据')],
        '方式', readonly=True)
    is_initial = fields.Boolean('首次导入', readonly=True)
    state = fields.Selection(
        [('running', '进行中'), ('done', '成功'), ('failed', '失败')], '结果', readonly=True)
    start_time = fields.Datetime('开始时间', readonly=True)
    end_time = fields.Datetime('结束时间', readonly=True)
    duration = fields.Float('耗时（秒）', readonly=True, digits=(16, 1))
    device_serial = fields.Char('设备序列号', readonly=True)
    time_offset = fields.Integer('时间差（秒）', readonly=True, help='设备时间减去服务器时间（校准前）。')
    time_adjusted = fields.Boolean('已校准时间', readonly=True)

    fetched_count = fields.Integer('读取条数', readonly=True)
    new_count = fields.Integer('新增条数', readonly=True)
    duplicate_count = fields.Integer('重复条数', readonly=True)
    skipped_count = fields.Integer('跳过条数', readonly=True, help='早于导入起始时间的记录。')
    future_count = fields.Integer('未来时间条数', readonly=True)
    new_user_count = fields.Integer('新发现用户', readonly=True)
    purged_count = fields.Integer('删除条数', readonly=True)
    user_id = fields.Many2one('res.users', '操作人', readonly=True, default=lambda self: self.env.user)
    message = fields.Text('信息', readonly=True)

    @api.depends('device_id', 'start_time', 'trigger')
    def _compute_display_name(self):
        labels = dict(self._fields['trigger'].selection)
        for log in self:
            log.display_name = '%s %s %s' % (
                log.device_id.name or '', labels.get(log.trigger, ''),
                fields.Datetime.to_string(log.start_time) or '')

    @api.model
    def _cron_cleanup(self):
        """删除超过保留天数的同步、校准时间日志。清理数据的日志和首次导入的日志永久保留。"""
        now = fields.Datetime.now()
        for company in self.env['res.company'].sudo().search([]):
            limit = now - timedelta(days=max(company.zk_log_keep_days or 90, 7))
            self.sudo().search([
                ('company_id', '=', company.id), ('trigger', '!=', 'purge'), ('is_initial', '=', False),
                ('state', '!=', 'running'), ('create_date', '<', limit),
            ]).unlink()
