from odoo import _, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    zk_start_date = fields.Date(related='company_id.zk_start_date', readonly=False)
    zk_lock_date = fields.Date(related='company_id.zk_lock_date', readonly=False)
    zk_late_grace = fields.Integer(related='company_id.zk_late_grace', readonly=False)
    zk_early_grace = fields.Integer(related='company_id.zk_early_grace', readonly=False)
    zk_debounce_minutes = fields.Integer(related='company_id.zk_debounce_minutes', readonly=False)
    zk_day_change_hour = fields.Float(related='company_id.zk_day_change_hour', readonly=False)
    zk_attendance_start_date = fields.Date(related='company_id.zk_attendance_start_date', readonly=False)
    zk_absent_half_minutes = fields.Integer(related='company_id.zk_absent_half_minutes', readonly=False)
    zk_absent_full_minutes = fields.Integer(related='company_id.zk_absent_full_minutes', readonly=False)
    zk_manual_monthly_limit = fields.Integer(related='company_id.zk_manual_monthly_limit', readonly=False)
    zk_manual_max_days = fields.Integer(related='company_id.zk_manual_max_days', readonly=False)
    zk_log_keep_days = fields.Integer(related='company_id.zk_log_keep_days', readonly=False)

    def action_zk_enable_attendance(self):
        """一键启用：从今天起生成出勤记录。"""
        self.ensure_one()
        company = self.company_id
        if not company.zk_start_date:
            raise UserError(_('请先设置“考勤计算起始日期”并保存。'))
        company.zk_attendance_start_date = max(fields.Date.context_today(self), company.zk_start_date)
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_zk_disable_attendance(self):
        """停用：不再生成和更新出勤记录，已生成的出勤记录保留。"""
        self.ensure_one()
        self.company_id.zk_attendance_start_date = False
        return {'type': 'ir.actions.client', 'tag': 'reload'}
