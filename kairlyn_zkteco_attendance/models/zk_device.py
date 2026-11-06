from datetime import timedelta, timezone
import logging
import time
from psycopg2 import errors
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.addons.base.models.res_partner import _tz_get

from ..services.connector import PyzkPullConnector, ZkConnectorError

_logger = logging.getLogger(__name__)

# pg_try_advisory_lock 的命名空间，避免与其他模块的锁冲突
_LOCK_NAMESPACE = 742019
_CREATE_BATCH = 1000
# 与其他事务同时修改同一行数据时 PostgreSQL 抛出的错误：回滚后重试即可
_CONCURRENCY_ERRORS = (errors.SerializationFailure, errors.DeadlockDetected)


class KairlynZkDevice(models.Model):
    _name = 'kairlyn.zk.device'
    _description = 'ZKTeco 考勤机'
    _inherit = ['mail.thread']
    _order = 'sequence, id'

    name = fields.Char('设备名称', required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True, tracking=True)
    company_id = fields.Many2one(
        'res.company', '公司', required=True, default=lambda self: self.env.company)

    # 连接参数
    connector_type = fields.Selection(
        [('pyzk', 'SDK 协议拉取（TCP/UDP 4370）')], '通讯方式',
        required=True, default='pyzk')
    ip_address = fields.Char('IP 地址', required=True, tracking=True)
    port = fields.Integer('端口', required=True, default=4370, tracking=True)
    comm_password = fields.Char(
        '通讯密码', copy=False, groups='hr_attendance.group_hr_attendance_manager',
        help='设备菜单中设置的通讯密码（数字），未设置时留空。')
    force_udp = fields.Boolean('强制使用 UDP')
    timeout = fields.Integer('超时（秒）', default=30)
    encoding = fields.Selection(
        [('gb18030', 'GB18030（GBK，国内 ZKTeco 设备常用）'), ('UTF-8', 'UTF-8')], '姓名编码',
        required=True, default='gb18030',
        help='读取设备用户姓名所用的编码。姓名显示乱码或为“NN-编号”时，切换后重新同步。')
    tz = fields.Selection(
        _tz_get, '设备时区', required=True,
        default=lambda self: self.env.user.tz or 'UTC',
        help='设备时钟当前所设的时区（不一定是公司所在地）。设备只记录不带时区的本地时间，'
             '同步时按此时区换算为 UTC 入库。设置正确时，“时间差（秒）”应接近 0。\n'
             '以后调整设备时钟的时区时：先同步一次 → 调好设备时钟 → 立即修改此项 → 再同步。')

    # 同步设置
    auto_sync = fields.Boolean('自动同步', default=False, tracking=True)
    sync_interval = fields.Integer('同步周期（分钟）', default=30)
    import_start = fields.Datetime(
        '导入起始时间', tracking=True,
        help='早于此时间的设备记录不会导入。用于清空测试数据后防止旧记录被再次导入，或新设备上线时确定起点。')
    last_success_at = fields.Datetime('最后成功同步', readonly=True, copy=False)
    alert_hours = fields.Integer(
        '失败提醒（小时）', default=6,
        help='开启自动同步后，超过这么多小时没有同步成功，就在本考勤机的消息中发提醒，关注者会收到通知；'
             '恢复后再发一条。0 表示不提醒。')
    alert_sent = fields.Boolean('已发失败提醒', readonly=True, copy=False)
    auto_sync_time = fields.Boolean(
        '同步时自动校准时间', default=False, tracking=True,
        help='每次同步下载完记录后，把设备时间校准为 Odoo 服务器时间（按设备时区换算）。'
             '这是本模块唯一会写入设备的操作。')
    time_sync_threshold = fields.Integer(
        '自动校准阈值（秒）', default=30,
        help='同步时，设备时间与服务器相差超过此秒数才自动校准，避免频繁写入设备。'
             '手动点击“校准设备时间”不受此限制。')
    time_sync_max_auto = fields.Integer(
        '自动校准上限（秒）', default=3600,
        help='相差超过此秒数时不自动校准，只在同步日志中报警。差这么多通常是设备时区或服务器时间设置错误，'
             '确认无误后可在表单上点击“校准设备时间”手动校准。')
    last_time_sync_at = fields.Datetime('最后校准时间', readonly=True, copy=False)
    next_sync_at = fields.Datetime('下次同步时间', readonly=True, copy=False)
    sync_requested = fields.Boolean('已请求同步', readonly=True, copy=False)
    sync_status = fields.Selection(
        [('idle', '空闲'), ('queued', '排队中'), ('running', '同步中')], '同步状态',
        default='idle', readonly=True, copy=False)

    # 最近结果
    last_sync_at = fields.Datetime('最后同步时间', readonly=True, copy=False)
    last_sync_state = fields.Selection(
        [('done', '成功'), ('failed', '失败')], '最后同步结果', readonly=True, copy=False)
    last_sync_message = fields.Text('最后同步信息', readonly=True, copy=False)

    # 设备信息（测试连接或同步时读取）
    serial_number = fields.Char('序列号', readonly=True, copy=False, tracking=True)
    firmware_version = fields.Char('固件版本', readonly=True, copy=False)
    platform = fields.Char('平台', readonly=True, copy=False)
    device_model_name = fields.Char('设备型号', readonly=True, copy=False)
    device_user_total = fields.Integer('设备用户数', readonly=True, copy=False)
    device_record_total = fields.Integer('设备记录数', readonly=True, copy=False)
    device_record_capacity = fields.Integer('记录容量', readonly=True, copy=False)
    time_offset = fields.Integer(
        '时间差（秒）', readonly=True, copy=False,
        help='设备时间减去服务器时间。正数表示设备时间偏快。')

    sync_cron_active = fields.Boolean('同步任务已启用', compute='_compute_sync_cron_active')

    punch_count = fields.Integer('打卡记录数', compute='_compute_counts')
    device_user_count = fields.Integer('设备用户数', compute='_compute_counts')
    unmapped_user_count = fields.Integer('待匹配用户', compute='_compute_counts')

    def _compute_counts(self):
        punch_data = dict(self.env['kairlyn.zk.punch']._read_group(
            [('device_id', 'in', self.ids)], ['device_id'], ['__count']))
        user_data = dict(self.env['kairlyn.zk.device.user']._read_group(
            [('device_id', 'in', self.ids)], ['device_id'], ['__count']))
        pending_data = dict(self.env['kairlyn.zk.device.user']._read_group(
            [('device_id', 'in', self.ids), ('state', '=', 'pending')], ['device_id'], ['__count']))
        for device in self:
            device.punch_count = punch_data.get(device, 0)
            device.device_user_count = user_data.get(device, 0)
            device.unmapped_user_count = pending_data.get(device, 0)

    def _compute_sync_cron_active(self):
        cron = self.env.ref('kairlyn_zkteco_attendance.ir_cron_kairlyn_zk_sync', raise_if_not_found=False)
        active = bool(cron and cron.sudo().active)
        for device in self:
            device.sync_cron_active = active

    def action_cancel_sync_queue(self):
        self.filtered(lambda d: d.sync_status == 'queued').write(
            {'sync_status': 'idle', 'sync_requested': False})

    @api.constrains('sync_interval', 'timeout', 'port', 'time_sync_threshold', 'time_sync_max_auto', 'alert_hours')
    def _check_numbers(self):
        for device in self:
            if device.time_sync_threshold < 1 or device.time_sync_max_auto <= device.time_sync_threshold:
                raise ValidationError(_('自动校准阈值至少 1 秒，且自动校准上限必须大于自动校准阈值。'))
            if device.alert_hours < 0:
                raise ValidationError(_('失败提醒的小时数不能为负数。'))
            if device.sync_interval < 5:
                raise ValidationError(_('同步周期不能小于 5 分钟。'))
            if device.timeout < 5:
                raise ValidationError(_('超时不能小于 5 秒。'))
            if not 0 < device.port < 65536:
                raise ValidationError(_('端口无效。'))

    @api.constrains('comm_password')
    def _check_comm_password(self):
        for device in self.sudo():
            password = device.comm_password
            if password and not (password.isdigit() and int(password) <= 999999):
                raise ValidationError(_('通讯密码只能是 0 到 999999 之间的数字。'))

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def _get_zoneinfo(self):
        self.ensure_one()
        try:
            return ZoneInfo(self.tz or 'UTC')
        except ZoneInfoNotFoundError:
            raise UserError(_('无法识别设备时区：%s', self.tz))

    def _local_to_utc(self, local_dt, zone):
        """设备本地时间（无时区）→ UTC（无时区，Odoo 存储格式）。"""
        return local_dt.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)

    def _get_connector(self):
        self.ensure_one()
        device = self.sudo()
        if device.connector_type == 'pyzk':
            return PyzkPullConnector(
                device.ip_address, port=device.port,
                password=int(device.comm_password) if device.comm_password else 0,
                timeout=device.timeout, force_udp=device.force_udp, encoding=device.encoding)
        raise UserError(_('不支持的通讯方式：%s', device.connector_type))

    def _info_vals(self, info, zone):
        vals = {
            'firmware_version': info.firmware_version,
            'platform': info.platform,
            'device_model_name': info.device_name,
            'device_user_total': info.user_count,
            'device_record_total': info.record_count,
            'device_record_capacity': info.record_capacity,
        }
        if info.device_time:
            device_utc = self._local_to_utc(info.device_time, zone)
            vals['time_offset'] = int((device_utc - fields.Datetime.now()).total_seconds())
        return vals

    def _check_serial(self, info):
        """防止 IP 被分配给另一台设备后，把别的设备数据记到这台名下。"""
        self.ensure_one()
        if self.serial_number and info.serial_number and self.serial_number != info.serial_number:
            raise UserError(_(
                '设备序列号不一致：记录为 %(old)s，实际连接到 %(new)s。\n'
                '请确认 IP 地址是否已分配给另一台设备。若确实更换了设备，请新建一个设备记录。',
                old=self.serial_number, new=info.serial_number))

    # ------------------------------------------------------------------
    # 按钮
    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        zone = self._get_zoneinfo()
        self._lock_for_button()
        try:
            with self._get_connector() as conn:
                info = conn.get_info()
        except ZkConnectorError as e:
            raise UserError(str(e))
        self._check_serial(info)
        vals = self._info_vals(info, zone)
        if not self.serial_number and info.serial_number:
            vals['serial_number'] = info.serial_number
        self.write(vals)
        message = _(
            '连接成功。序列号 %(sn)s，用户 %(users)s 个，记录 %(records)s 条，时间差 %(offset)s 秒。',
            sn=info.serial_number or '-', users=info.user_count, records=info.record_count,
            offset=vals.get('time_offset', '-'))
        offset = vals.get('time_offset', 0)
        if abs(offset) >= 3600:
            message += _('\n设备时间与服务器相差约 %s 小时，请检查“设备时区”是否与设备时钟所设的时区一致。',
                         round(offset / 3600, 1))
            return self._notify(_('测试连接'), message, 'warning')
        return self._notify(_('测试连接'), message, 'success')

    def action_match_users_by_name(self):
        users = self.env['kairlyn.zk.device.user'].search(
            [('device_id', 'in', self.ids), ('state', '=', 'pending')])
        return users._match_by_name()

    def _is_neutralized(self):
        """复制时做过中和的测试数据库，禁止写入设备。"""
        return bool(self.env['ir.config_parameter'].sudo().get_param('database.is_neutralized'))

    def _adjust_device_time(self, conn, zone, force=False):
        """把设备时间校准为 Odoo 服务器时间。

        :param force: 手动校准时为 True，不受校准阈值和自动校准上限限制
        :return: {'adjusted': 是否已校准, 'offset': 校准前相差秒数, 'after': 校准后相差秒数, 'note': 说明}
        """
        self.ensure_one()
        result = {'adjusted': False, 'offset': None, 'after': None, 'note': ''}
        if self._is_neutralized():
            result['note'] = _('测试数据库（已中和）不允许修改设备时间。')
            return result
        offset = int((self._local_to_utc(conn.get_time(), zone) - fields.Datetime.now()).total_seconds())
        result['offset'] = offset
        if not force and abs(offset) < self.time_sync_threshold:
            return result
        if not force and abs(offset) > self.time_sync_max_auto:
            result['note'] = _(
                '设备时间与服务器相差 %s 秒，超过自动校准上限，未自动校准。'
                '请先确认“设备时区”和服务器时间正确，再在考勤机表单点击“校准设备时间”。', offset)
            return result
        target = fields.Datetime.now().replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)
        conn.set_time(target)
        after = int((self._local_to_utc(conn.get_time(), zone) - fields.Datetime.now()).total_seconds())
        result.update(adjusted=True, after=after, note=_(
            '已校准设备时间：校准前相差 %(before)s 秒，校准后相差 %(after)s 秒。', before=offset, after=after))
        return result

    def _apply_time_result(self, result):
        """记录一次成功的校准。"""
        self.ensure_one()
        self.write({'last_time_sync_at': fields.Datetime.now(), 'time_offset': result['after']})
        self.message_post(body=result['note'])

    def action_open_time_wizard(self):
        """读取设备时间，显示与服务器的时间差，确认后再校准。"""
        self.ensure_one()
        if self._is_neutralized():
            raise UserError(_('这是测试数据库（已中和），不允许修改考勤机的时间。'))
        zone = self._get_zoneinfo()
        self._lock_for_button()
        try:
            with self._get_connector() as conn:
                self._check_serial(conn.get_info())
                device_time = conn.get_time()
        except ZkConnectorError as e:
            raise UserError(str(e))
        now = fields.Datetime.now()
        server_time = now.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)
        wizard = self.env['kairlyn.zk.time.wizard'].create({
            'device_id': self.id,
            'tz': self.tz,
            'device_time': fields.Datetime.to_string(device_time),
            'server_time': fields.Datetime.to_string(server_time),
            'offset': int((self._local_to_utc(device_time, zone) - now).total_seconds()),
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('校准设备时间'),
            'res_model': 'kairlyn.zk.time.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_sync_device_time(self):
        return self._notify(_('校准设备时间'), self._calibrate_now(), 'success')

    def _calibrate_now(self):
        """立即把设备时间校准为服务器时间（不受校准阈值限制），返回说明。"""
        self.ensure_one()
        if self._is_neutralized():
            raise UserError(_('这是测试数据库（已中和），不允许修改考勤机的时间。'))
        zone = self._get_zoneinfo()
        started = fields.Datetime.now()
        self._lock_for_button()
        try:
            with self._get_connector() as conn:
                self._check_serial(conn.get_info())
                result = self._adjust_device_time(conn, zone, force=True)
        except ZkConnectorError as e:
            raise UserError(str(e))
        self._apply_time_result(result)
        self.env['kairlyn.zk.sync.log'].sudo().create({
            'device_id': self.id,
            'trigger': 'time',
            'state': 'done',
            'start_time': started,
            'end_time': fields.Datetime.now(),
            'time_offset': result['offset'],
            'time_adjusted': True,
            'message': result['note'],
        })
        return result['note']

    def action_request_sync(self):
        for device in self:
            if not device.active:
                raise UserError(_('设备 %s 已归档，不能同步。', device.name))
        self.write({'sync_requested': True, 'sync_status': 'queued'})
        self.env.ref('kairlyn_zkteco_attendance.ir_cron_kairlyn_zk_sync')._trigger()
        return self._notify(
            _('已加入同步队列'),
            _('同步将在后台进行，可在“同步日志”中查看进度。首次导入数据较多，可能需要几分钟。'),
            'info')

    def _notify(self, title, message, kind):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': kind,
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def action_view_punches(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'kairlyn_zkteco_attendance.action_kairlyn_zk_punch')
        action['domain'] = [('device_id', '=', self.id)]
        action['context'] = {'default_device_id': self.id}
        return action

    def action_view_device_users(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'kairlyn_zkteco_attendance.action_kairlyn_zk_device_user')
        action['domain'] = [('device_id', '=', self.id)]
        return action

    def action_view_sync_logs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'kairlyn_zkteco_attendance.action_kairlyn_zk_sync_log')
        action['domain'] = [('device_id', '=', self.id)]
        return action

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------
    @api.model
    def _cron_sync_devices(self):
        now = fields.Datetime.now()
        devices = self.sudo().search([
            ('active', '=', True),
            '|', ('sync_requested', '=', True),
            '&', ('auto_sync', '=', True),
            '|', ('next_sync_at', '=', False), ('next_sync_at', '<=', now),
        ])
        for device in devices:
            trigger = 'manual' if device.sync_requested else 'cron'
            device._run_sync(trigger)

    def _try_lock(self):
        self.env.cr.execute('SELECT pg_try_advisory_lock(%s, %s)', (_LOCK_NAMESPACE, self.id))
        return self.env.cr.fetchone()[0]

    def _unlock(self):
        self.env.cr.execute('SELECT pg_advisory_unlock(%s, %s)', (_LOCK_NAMESPACE, self.id))

    def _lock_for_button(self):
        """测试连接、校准时间、清理数据不能与正在进行的同步同时进行。

        使用事务级的锁：请求结束（提交或回滚）时自动释放，出错也不会一直占着。
        """
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s, %s)', (_LOCK_NAMESPACE, self.id))
        if not self.env.cr.fetchone()[0]:
            raise UserError(_('考勤机 %s 正在同步，请等同步结束后再试。', self.name))

    def _commit(self):
        # 在 cron 中会同时上报进度；在其他场景下仅提交
        self.env['ir.cron']._commit_progress()

    def _retry_on_conflict(self, func, *args):
        """执行 func 并提交事务；遇到数据库并发冲突时回滚，用新的数据快照再执行一次。

        同步期间若有人同时修改了同一行数据（例如在界面上操作这台设备、修改用户映射），
        PostgreSQL 会报 "could not serialize access due to concurrent update"。
        """
        for attempt in (1, 2):
            try:
                result = func(*args)
                self._commit()
                return result
            except _CONCURRENCY_ERRORS:
                self.env.cr.rollback()
                if attempt == 2:
                    raise
                _logger.info('设备 %s 同步时遇到数据库并发冲突，自动重试', self.name)

    def _run_sync(self, trigger='cron'):
        """同步一台设备。会分批提交事务，只能在 cron 中调用。"""
        self.ensure_one()
        device = self.sudo()
        if not device._try_lock():
            _logger.info('设备 %s 正在同步或正在被其他操作使用，跳过', device.name)
            return
        try:
            log = device._retry_on_conflict(device._start_sync, trigger)
            started = time.monotonic()
            error = None
            try:
                snapshot, time_result = device._fetch_from_device()
                # 下载可能要几十秒：先结束下载前开始的事务，入库时使用新的数据快照
                self._commit()
                device._retry_on_conflict(device._store_snapshot, log, snapshot, time_result)
            except Exception as e:
                self.env.cr.rollback()
                _logger.warning('设备 %s 同步失败', device.name, exc_info=True)
                error = e
            device._retry_on_conflict(device._finish_sync, log, started, error)
        finally:
            # 会话级的锁不会随事务回滚释放；先回滚，保证出错后也能执行解锁
            self.env.cr.rollback()
            self._unlock()

    def _start_sync(self, trigger):
        log = self.env['kairlyn.zk.sync.log'].create({
            'device_id': self.id,
            'trigger': trigger,
            'is_initial': not self.env['kairlyn.zk.punch'].search_count(
                [('device_id', '=', self.id)], limit=1),
            'state': 'running',
            'start_time': fields.Datetime.now(),
        })
        self.write({'sync_status': 'running', 'sync_requested': False})
        return log

    def _finish_sync(self, log, started, error):
        now = fields.Datetime.now()
        log_vals = {'end_time': now, 'duration': time.monotonic() - started}
        device_vals = {
            'sync_status': 'idle',
            'last_sync_at': now,
            'next_sync_at': now + timedelta(minutes=self.sync_interval),
        }
        if error:
            if isinstance(error, _CONCURRENCY_ERRORS):
                message = _('与其他操作同时修改了相同的数据（数据库并发冲突），自动重试后仍未成功。'
                            '已保存的记录不会丢失，下次同步会继续。')
            elif isinstance(error, (UserError, ZkConnectorError)):
                message = str(error)
            else:
                message = repr(error)
            log.write(dict(log_vals, state='failed', message=message))
            device_vals.update(last_sync_state='failed', last_sync_message=message)
            since = self.last_success_at or self.create_date
            if self.auto_sync and self.alert_hours and not self.alert_sent and since \
                    and now - since >= timedelta(hours=self.alert_hours):
                self.message_post(
                    body=_('考勤机 %(name)s 已超过 %(hours)s 小时没有同步成功。最后一次失败原因：%(message)s',
                           name=self.name, hours=self.alert_hours, message=message),
                    message_type='comment', subtype_xmlid='mail.mt_comment')
                device_vals['alert_sent'] = True
        else:
            log.write(dict(log_vals, state='done'))
            device_vals.update(last_sync_state='done', last_success_at=now,
                               last_sync_message=log.message or _('新增 %s 条记录', log.new_count))
            if self.alert_sent:
                device_vals['alert_sent'] = False
                self.message_post(body=_('考勤机 %s 已恢复同步。', self.name),
                                  message_type='comment', subtype_xmlid='mail.mt_comment')
        self.write(device_vals)

    def _fetch_from_device(self):
        """连接设备，下载用户和打卡记录；开启了自动校准时顺便校准设备时间。不写数据库。"""
        self.ensure_one()
        zone = self._get_zoneinfo()
        time_result = None
        with self._get_connector() as conn:
            snapshot = conn.snapshot()
            if self.auto_sync_time:
                self._check_serial(snapshot.info)
                try:
                    time_result = self._adjust_device_time(conn, zone)
                except ZkConnectorError as e:  # 校准失败不影响已下载的记录
                    time_result = {'adjusted': False, 'offset': None, 'after': None,
                                   'note': _('校准设备时间失败：%s', e)}
        return snapshot, time_result

    def _store_snapshot(self, log, snapshot, time_result):
        """把下载的数据写入数据库，打卡记录分批提交。

        冲突后重试时，本次同步已提交的打卡（own）既不重复写入，也不计为“重复”，统计数字保持一致。
        """
        self.ensure_one()
        zone = self._get_zoneinfo()
        info = snapshot.info
        self._check_serial(info)
        vals = self._info_vals(info, zone)
        if not self.serial_number and info.serial_number:
            vals['serial_number'] = info.serial_number
        self.write(vals)

        new_user_count, user_map = self.env['kairlyn.zk.device.user']._sync_from_device(
            self, snapshot.users, snapshot.punches)

        # 已入库的键，用于去重
        self.env.cr.execute(
            'SELECT user_code, punch_time, sync_log_id = %s FROM kairlyn_zk_punch WHERE device_id = %s',
            (log.id, self.id))
        existing, own = set(), set()
        for user_code, punch_time, is_own in self.env.cr.fetchall():
            (own if is_own else existing).add((user_code, punch_time))

        now = fields.Datetime.now()
        future_limit = now + timedelta(hours=1)
        skipped = duplicates = future = 0
        to_create = []
        seen = set()
        for punch in snapshot.punches:
            punch_utc = self._local_to_utc(punch.local_time, zone)
            if self.import_start and punch_utc < self.import_start:
                skipped += 1
                continue
            key = (punch.user_code, punch_utc)
            if key in existing or key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if punch_utc > future_limit:
                future += 1
            if key in own:
                continue
            to_create.append({
                'device_id': self.id,
                'device_user_id': user_map[punch.user_code].id,
                'user_code': punch.user_code,
                'punch_time': punch_utc,
                'device_local_time': punch.local_time.strftime('%Y-%m-%d %H:%M:%S'),
                'verify_type': punch.verify_type,
                'punch_state': punch.punch_state,
                'device_uid': punch.device_uid,
                'sync_log_id': log.id,
            })

        log.write({
            'fetched_count': len(snapshot.punches),
            'duplicate_count': duplicates,
            'skipped_count': skipped,
            'future_count': future,
            'new_user_count': log.new_user_count + new_user_count,
            'new_count': len(own),
            'device_serial': info.serial_number,
            'time_offset': vals.get('time_offset', 0),
        })
        self._commit()

        Punch = self.env['kairlyn.zk.punch']
        Day = self.env['kairlyn.zk.attendance.day']
        created = 0
        for start in range(0, len(to_create), _CREATE_BATCH):
            batch = to_create[start:start + _CREATE_BATCH]
            Punch.create(batch)
            # 涉及的员工从这批最早一条打卡的前一天起重新计算。请求与打卡一起提交，入库的打卡一定会被计算
            employees = self.env['hr.employee'].union(
                *(user_map[v['user_code']].employee_id for v in batch))
            if employees:
                earliest = min(v['punch_time'] for v in batch).date() - timedelta(days=1)
                Day._queue_recompute(employees, earliest, trigger=False)
            created += len(batch)
            log.write({'new_count': len(own) + created})
            self._commit()

        self.env['kairlyn.zk.device.user']._refresh_stats(self)
        if own or to_create:
            Day._trigger_compute()

        notes = []
        if future:
            notes.append(_('有 %s 条记录的时间晚于服务器当前时间，请检查设备时钟。', future))
        if time_result and time_result['adjusted']:
            self._apply_time_result(time_result)
            log.write({'time_adjusted': True})
        elif abs(vals.get('time_offset', 0)) > 300:
            notes.append(_('设备时间与服务器相差 %s 秒。', vals['time_offset']))
        if time_result and time_result['note']:
            notes.append(time_result['note'])
        if notes:
            log.write({'message': '\n'.join(notes)})
