import re

from markupsafe import Markup

from odoo import _, api, fields, models


def _normalize_name(name):
    """比较姓名时忽略所有空白（含全角空格）和大小写。"""
    return re.sub(r'\s+', '', name or '').casefold()


class KairlynZkDeviceUser(models.Model):
    """设备上的用户编号与 Odoo 员工的对应关系。

    设备用户被删除（例如员工离职）后，这里的记录和映射仍然保留，
    以保证历史打卡记录可以追溯到人。
    """
    _name = 'kairlyn.zk.device.user'
    _description = 'ZKTeco 设备用户映射'
    _inherit = ['mail.thread']
    _order = 'device_id, user_code_int, user_code'

    device_id = fields.Many2one(
        'kairlyn.zk.device', '设备', required=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(related='device_id.company_id', store=True)
    user_code = fields.Char('设备用户编号', required=True, readonly=True, index=True)
    user_code_int = fields.Integer(compute='_compute_user_code_int', store=True)
    name = fields.Char('设备上的姓名', readonly=True,
                       help='最后一次从设备读到的姓名。设备上已删除的用户保留最后的姓名。')
    note = fields.Char('备注', help='可补充离职人员的姓名等历史信息。')
    device_uid = fields.Integer('设备内部序号', readonly=True)
    privilege = fields.Integer('设备权限', readonly=True)

    employee_id = fields.Many2one(
        'hr.employee', '员工', tracking=True, index=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        context={'active_test': False})
    ignored = fields.Boolean('忽略', tracking=True,
                             help='测试用户、管理员账号等不需要关联员工的设备用户。')
    state = fields.Selection(
        [('pending', '待匹配'), ('mapped', '已匹配'), ('ignored', '已忽略')], '状态',
        compute='_compute_state', store=True, index=True)

    present_on_device = fields.Boolean('设备上存在', readonly=True, default=True)
    first_seen_at = fields.Datetime('首次发现', readonly=True)
    last_seen_at = fields.Datetime('最后在设备上发现', readonly=True)
    name_changed = fields.Boolean(
        '姓名有变化', readonly=True,
        help='设备上同一编号的姓名发生了变化，请核实是否被重新分配给其他人。'
             '修改设备的“姓名编码”后，所有姓名都会显示为有变化，属正常现象。')
    reenrolled = fields.Boolean(
        '重新登记过', readonly=True,
        help='该编号在设备上被删除后又重新出现，或设备内部序号发生了变化，'
             '说明该用户在设备上被删除后重新登记过。请核实是否仍是同一人。')

    punch_count = fields.Integer('打卡次数', readonly=True)
    first_punch_at = fields.Datetime('最早打卡', readonly=True)
    last_punch_at = fields.Datetime('最近打卡', readonly=True)

    _device_code_uniq = models.Constraint(
        'UNIQUE(device_id, user_code)',
        '同一设备的用户编号不能重复。',
    )

    @api.depends('user_code')
    def _compute_user_code_int(self):
        for rec in self:
            code = rec.user_code or ''
            rec.user_code_int = int(code) if code.isdigit() and len(code) < 10 else 0

    @api.depends('employee_id', 'ignored')
    def _compute_state(self):
        for rec in self:
            if rec.employee_id:
                rec.state = 'mapped'
            elif rec.ignored:
                rec.state = 'ignored'
            else:
                rec.state = 'pending'

    @api.depends('device_id', 'user_code', 'name', 'employee_id')
    def _compute_display_name(self):
        for rec in self:
            label = rec.employee_id.name or rec.name or rec.note or ''
            rec.display_name = '[%s] %s %s' % (rec.device_id.name or '', rec.user_code or '', label)

    def write(self, vals):
        affected = self.employee_id if 'employee_id' in vals else self.env['hr.employee']
        res = super().write(vals)
        if 'employee_id' in vals:
            # 映射变化后，原员工和新员工的每日考勤都要重新计算
            self.env['kairlyn.zk.attendance.day']._queue_recompute(affected | self.employee_id)
        return res

    def action_mark_reviewed(self):
        for rec in self.filtered(lambda r: r.name_changed or r.reenrolled):
            rec.message_post(body=_('%s 已核实该设备用户的变化。', self.env.user.name))
        self.write({'name_changed': False, 'reenrolled': False})

    def action_match_by_name(self):
        """按设备上的姓名匹配员工。

        勾选了记录时只处理所选记录；未勾选时处理当前列表筛选范围内的全部待匹配用户。
        """
        if self:
            records = self
        else:
            active_domain = self.env.context.get('active_domain')
            domain = list(active_domain) if isinstance(active_domain, (list, tuple)) else []
            records = self.search(domain + [('state', '=', 'pending')])
        return records._match_by_name()

    def _match_by_name(self):
        pending = self.filtered(lambda r: r.state == 'pending')
        Employee = self.env['hr.employee'].sudo().with_context(active_test=False)
        by_company = {}
        for company in pending.company_id:
            index = {}
            for employee in Employee.search([('company_id', '=', company.id)]):
                index.setdefault(_normalize_name(employee.name), Employee.browse())
                index[_normalize_name(employee.name)] |= employee
            by_company[company.id] = index

        matched = ambiguous = not_found = no_name = 0
        for rec in pending:
            key = _normalize_name(rec.name)
            if not key:
                no_name += 1
                continue
            employees = by_company[rec.company_id.id].get(key, Employee.browse())
            if len(employees) == 1:
                rec.employee_id = employees.id
                rec.message_post(body=_('按设备上的姓名“%s”自动匹配到员工。', rec.name))
                matched += 1
            elif employees:
                ambiguous += 1
            else:
                not_found += 1

        if not pending:
            message = _('没有待匹配的设备用户。')
        else:
            message = _(
                '已匹配 %(matched)s 个；有重名需手动处理 %(ambiguous)s 个；'
                '未找到同名员工 %(not_found)s 个；设备上无姓名 %(no_name)s 个。',
                matched=matched, ambiguous=ambiguous, not_found=not_found, no_name=no_name)
        kind = 'success' if matched and not (ambiguous or not_found or no_name) else 'info'
        return self.env['kairlyn.zk.device']._notify(_('按姓名匹配员工'), message, kind)

    def action_view_punches(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'kairlyn_zkteco_attendance.action_kairlyn_zk_punch')
        action['domain'] = [('device_user_id', '=', self.id)]
        return action

    # ------------------------------------------------------------------
    # 同步时调用
    # ------------------------------------------------------------------
    @api.model
    def _sync_from_device(self, device, device_users, punches):
        """根据设备用户列表和打卡记录中出现的编号，维护设备用户。

        :return: (新增数量, {user_code: 记录})
        """
        now = fields.Datetime.now()
        existing = {
            rec.user_code: rec
            for rec in self.with_context(active_test=False).search([('device_id', '=', device.id)])
        }
        current_codes = set()
        to_create = []
        # 循环变量不要叫 user / uid：Odoo 的翻译函数 _() 会读取调用处名为 user、uid 的局部变量
        for device_user in device_users:
            current_codes.add(device_user.user_code)
            rec = existing.get(device_user.user_code)
            if rec:
                vals = {
                    'present_on_device': True,
                    'last_seen_at': now,
                    'device_uid': device_user.device_uid,
                    'privilege': device_user.privilege,
                }
                if not rec.present_on_device:
                    reason = _('该编号此前在设备上已不存在，现在重新出现。')
                elif rec.device_uid and device_user.device_uid and rec.device_uid != device_user.device_uid:
                    reason = _('设备内部序号由 %(old)s 变为 %(new)s，说明该用户在设备上被删除后重新登记过。',
                               old=rec.device_uid, new=device_user.device_uid)
                else:
                    reason = ''
                if reason:
                    vals['reenrolled'] = True
                    rec.message_post(body=reason + _('请核实是否仍是同一人，确认后点击“已核实”。'))
                if device_user.name and device_user.name != rec.name:
                    if rec.name:
                        vals['name_changed'] = True
                        rec.message_post(body=Markup(_(
                            '设备上的姓名由 <b>%(old)s</b> 变为 <b>%(new)s</b>，请核实该编号是否已分配给其他人。'
                        )) % {'old': rec.name, 'new': device_user.name})
                    vals['name'] = device_user.name
                rec.write(vals)
            else:
                to_create.append({
                    'device_id': device.id,
                    'user_code': device_user.user_code,
                    'name': device_user.name,
                    'device_uid': device_user.device_uid,
                    'privilege': device_user.privilege,
                    'present_on_device': True,
                    'first_seen_at': now,
                    'last_seen_at': now,
                })

        # 打卡记录中出现、但设备用户列表里已经没有的编号（已删除的用户）
        listed = {vals['user_code'] for vals in to_create} | set(existing)
        for code in sorted({p.user_code for p in punches} - listed):
            to_create.append({
                'device_id': device.id,
                'user_code': code,
                'present_on_device': False,
                'first_seen_at': now,
            })

        gone = [rec for code, rec in existing.items() if code not in current_codes and rec.present_on_device]
        if gone:
            self.browse([rec.id for rec in gone]).write({'present_on_device': False})

        created = self.create(to_create) if to_create else self.browse()
        result = dict(existing)
        result.update({rec.user_code: rec for rec in created})
        return len(created), result

    @api.model
    def _refresh_stats(self, devices):
        """用一条 SQL 重新统计每个设备用户的打卡次数和时间范围。"""
        if not devices:
            return
        self.env['kairlyn.zk.punch'].flush_model()
        self.flush_model()
        self.env.cr.execute("""
            UPDATE kairlyn_zk_device_user du
               SET punch_count = COALESCE(s.cnt, 0),
                   first_punch_at = s.first_at,
                   last_punch_at = s.last_at
              FROM kairlyn_zk_device_user du2
         LEFT JOIN (
                SELECT device_user_id, COUNT(*) AS cnt,
                       MIN(punch_time) AS first_at, MAX(punch_time) AS last_at
                  FROM kairlyn_zk_punch
                 WHERE device_id = ANY(%s)
              GROUP BY device_user_id
             ) s ON s.device_user_id = du2.id
             WHERE du.id = du2.id AND du2.device_id = ANY(%s)
        """, (devices.ids, devices.ids))
        self.invalidate_model(['punch_count', 'first_punch_at', 'last_punch_at'])
