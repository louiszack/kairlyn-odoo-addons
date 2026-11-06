from odoo import _, fields, models


class KairlynZkTimeWizard(models.TransientModel):
    """校准前先显示设备时间和服务器时间，确认后立即校准。"""
    _name = 'kairlyn.zk.time.wizard'
    _description = '校准考勤机时间'

    device_id = fields.Many2one('kairlyn.zk.device', '考勤机', required=True, readonly=True)
    tz = fields.Char('设备时区', readonly=True)
    device_time = fields.Char('设备时间', readonly=True)
    server_time = fields.Char('服务器时间', readonly=True, help='Odoo 服务器当前时间，已按设备时区换算。')
    offset = fields.Integer('相差（秒）', readonly=True, help='设备时间减去服务器时间。正数表示设备快了。')

    def action_calibrate(self):
        self.ensure_one()
        note = self.device_id._calibrate_now()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('校准设备时间'),
                'message': note,
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
