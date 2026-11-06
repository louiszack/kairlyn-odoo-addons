from odoo import fields, models


class KairlynZkRecomputeRequest(models.Model):
    """待重新计算每日考勤的请求队列。

    同步、修改映射、修改考勤规则等操作只“新增”请求，计算任务处理完再删除，
    双方不会同时修改同一行数据，避免数据库并发冲突（不在员工档案上做标记）。
    """
    _name = 'kairlyn.zk.recompute.request'
    _description = 'ZKTeco 每日考勤待重算'
    _order = 'id'

    employee_id = fields.Many2one('hr.employee', '员工', required=True, index=True, ondelete='cascade')
    date_from = fields.Date('起始日期', required=True)
