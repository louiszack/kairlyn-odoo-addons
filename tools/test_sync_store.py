"""离线测试同步入库与数据库并发冲突的自动重试（不需要 Odoo 和数据库，也不连接考勤机）。

运行：python3 tools/test_sync_store.py
用一个模拟事务的内存“数据库”，在入库过程的不同位置制造并发冲突，验证：
重试后打卡不重复、不丢失，统计数字一致，待重算请求与打卡一起提交，连续冲突两次才报失败，出错后仍会解锁。
"""
import copy
import importlib
import logging
import os
import sys
import time
import types
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logging.disable(logging.CRITICAL)  # 被测代码会记录警告和堆栈，这里不需要
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'kairlyn_zkteco_attendance')
NOW = datetime(2026, 10, 18, 6, 0, 0)  # Odoo 服务器时间（UTC，无时区）


class _Any:
    def __init__(self, *args, **kwargs):
        pass


class _Datetime(_Any):
    now = staticmethod(lambda: NOW)


fields_mod = types.ModuleType('odoo.fields')
for _name in ['Many2one', 'One2many', 'Many2many', 'Date', 'Char', 'Text', 'Boolean', 'Float',
              'Integer', 'Selection']:
    setattr(fields_mod, _name, _Any)
fields_mod.Datetime = _Datetime
exc_mod = types.ModuleType('odoo.exceptions')
exc_mod.UserError = exc_mod.ValidationError = type('UserError', (Exception,), {})
odoo = types.ModuleType('odoo')
odoo._ = lambda text, *args, **kwargs: (text % (kwargs or args[0])) if (args or kwargs) else text
odoo.fields, odoo.exceptions = fields_mod, exc_mod
odoo.models = types.SimpleNamespace(Model=object, Constraint=_Any)
odoo.api = types.SimpleNamespace(depends=lambda *a: (lambda f: f), model=lambda f: f,
                                 model_create_multi=lambda f: f,
                                 constrains=lambda *a: (lambda f: f))
partner_mod = types.ModuleType('odoo.addons.base.models.res_partner')
partner_mod._tz_get = lambda self: []
pg_errors = types.ModuleType('psycopg2.errors')
pg_errors.SerializationFailure = type('SerializationFailure', (Exception,), {})
pg_errors.DeadlockDetected = type('DeadlockDetected', (Exception,), {})
psycopg2_mod = types.ModuleType('psycopg2')
psycopg2_mod.errors = pg_errors
sys.modules.update({
    'odoo': odoo, 'odoo.fields': fields_mod, 'odoo.exceptions': exc_mod,
    'odoo.addons': types.ModuleType('odoo.addons'), 'odoo.addons.base': types.ModuleType('odoo.addons.base'),
    'odoo.addons.base.models': types.ModuleType('odoo.addons.base.models'),
    'odoo.addons.base.models.res_partner': partner_mod,
    'psycopg2': psycopg2_mod, 'psycopg2.errors': pg_errors,
})
# 只导入 zk_device.py，不执行 models/__init__.py
root_pkg = types.ModuleType('kz'); root_pkg.__path__ = [ROOT]
models_pkg = types.ModuleType('kz.models'); models_pkg.__path__ = [os.path.join(ROOT, 'models')]
sys.modules.update({'kz': root_pkg, 'kz.models': models_pkg})
zk_device = importlib.import_module('kz.models.zk_device')
connector = importlib.import_module('kz.services.connector')
Device = zk_device.KairlynZkDevice
zk_device._CREATE_BATCH = 2  # 4 条新打卡分两批提交
Conflict = pg_errors.SerializationFailure
ZONE = ZoneInfo('Asia/Shanghai')
EMPLOYEE_OF = {'1': 'E1', '2': 'E2'}  # 设备用户编号 → 员工；9 号没有关联员工


class Employees(frozenset):
    """代替 hr.employee 记录集。"""

    def union(self, *others):
        return Employees(frozenset().union(self, *others))


class FakeDB:
    """work 是当前事务看到的数据，committed 是已提交的数据；在 conflicts 指定的位置制造并发冲突。"""

    def __init__(self, conflicts=()):
        self.committed = {'punches': {}, 'users': set(), 'requests': [], 'triggers': 0, 'logs': {}}
        self.work = copy.deepcopy(self.committed)
        self.conflicts = set(conflicts)  # {(操作, 第几次)}
        self.calls = Counter()

    def step(self, name):
        self.calls[name] += 1
        if (name, self.calls[name]) in self.conflicts:
            raise Conflict('could not serialize access due to concurrent update')

    def commit(self):
        self.step('commit')  # 冲突通常在提交前写入数据库时发生
        self.committed = copy.deepcopy(self.work)

    def rollback(self):
        self.work = copy.deepcopy(self.committed)


class FakeLog:
    def __init__(self, db, log_id):
        self._db, self.id = db, log_id

    def __getattr__(self, name):
        return self._db.work['logs'].get(self.id, {}).get(name, 0)

    def write(self, vals):
        self._db.work['logs'].setdefault(self.id, {}).update(vals)


def make_device(db):
    class Cr:
        rows = []

        def execute(self, query, params):
            log_id, device_id = params
            self.rows = [(code, t, lid == log_id) for (code, t), lid in db.work['punches'].items()]

        def fetchall(self):
            return self.rows

        def rollback(self):
            db.rollback()

    class DeviceUsers:
        def _sync_from_device(self, device, device_users, punches):
            db.step('users')
            codes = {u.user_code for u in device_users} | {p.user_code for p in punches}
            new = codes - db.work['users']
            db.work['users'] |= new
            return len(new), {code: types.SimpleNamespace(
                id=int(code), employee_id=Employees({EMPLOYEE_OF[code]} if code in EMPLOYEE_OF else ()))
                for code in codes}

        def _refresh_stats(self, device):
            db.step('refresh')

    class Punches:
        def create(self, vals_list):
            db.step('create')
            for vals in vals_list:
                key = (vals['user_code'], vals['punch_time'])
                if key in db.work['punches']:
                    raise AssertionError('违反唯一约束：重复写入 %s' % (key,))
                db.work['punches'][key] = vals['sync_log_id']

    class Days:
        def _queue_recompute(self, employees, date_from=None, trigger=True):
            db.step('queue')
            db.work['requests'] += [(e, date_from) for e in sorted(employees)]

        def _trigger_compute(self):
            db.work['triggers'] += 1

    class Env(dict):
        cr = Cr()

    dev = types.SimpleNamespace(id=1, name='考勤机', serial_number='SN1', import_start=None)
    dev.env = Env({'kairlyn.zk.device.user': DeviceUsers(), 'kairlyn.zk.punch': Punches(),
                   'kairlyn.zk.attendance.day': Days(), 'hr.employee': Employees()})
    dev.ensure_one = lambda: None
    dev._get_zoneinfo = lambda: ZONE
    dev._check_serial = lambda info: None
    dev._info_vals = lambda info, zone: {'time_offset': 0}
    dev.write = lambda vals: db.step('device')
    dev._local_to_utc = lambda local_dt, zone: Device._local_to_utc(dev, local_dt, zone)
    dev._commit = db.commit
    dev._store_snapshot = lambda *args: Device._store_snapshot(dev, *args)
    return dev


def punch(code, day, hour, minute):
    return connector.DevicePunch(user_code=code, local_time=datetime(2026, 10, day, hour, minute))


SNAPSHOT = connector.DeviceSnapshot(
    info=connector.DeviceInfo(serial_number='SN1'),
    users=[connector.DeviceUser('1', '张三', 1), connector.DeviceUser('2', '李四', 2)],
    punches=[punch('9', 15, 8, 0),                         # 9 号已从设备删除，只在打卡记录中出现
             punch('1', 16, 7, 57), punch('1', 16, 7, 57),  # 同一条记录下载到两次
             punch('2', 16, 8, 1), punch('1', 16, 17, 5)])
COUNT_FIELDS = ['fetched_count', 'duplicate_count', 'skipped_count', 'future_count', 'new_user_count', 'new_count']
EXPECTED_COUNTS = [5, 1, 0, 0, 3, 4]
# 第一批（9 号 10/15 08:00、1 号 10/16 07:57）从 10/14 起；第二批（2 号、1 号 10/16）从 10/15 起（UTC 日期减一天）
EXPECTED_REQUESTS = [('E1', date(2026, 10, 14)), ('E1', date(2026, 10, 15)), ('E2', date(2026, 10, 15))]


def store(db, log_id=7):
    """调用真实的 _retry_on_conflict + _store_snapshot；返回（已提交的数据，抛出的冲突）。"""
    dev = make_device(db)
    try:
        Device._retry_on_conflict(dev, dev._store_snapshot, FakeLog(db, log_id), SNAPSHOT, None)
    except Conflict as e:
        return db.committed, e
    return db.committed, None


def summary(state, log_id=7):
    log = state['logs'].get(log_id, {})
    return ([log.get(f) for f in COUNT_FIELDS], len(state['punches']),
            sorted(state['requests']), state['triggers'])


failures = 0


def check(name, got, expected):
    global failures
    ok = got == expected
    failures += not ok
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else f'\n     got={got!r}\n     expected={expected!r}'))


GOOD = (EXPECTED_COUNTS, 4, EXPECTED_REQUESTS, 1)

state, error = store(FakeDB())
check('没有冲突：统计、打卡、待重算请求、触发计算', (summary(state), error), (GOOD, None))

for name, conflicts in [
    ('冲突发生在第一次提交前（还没写入任何打卡）', [('commit', 1)]),
    ('冲突发生在提交第二批打卡时（第一批已提交）', [('commit', 3)]),
    ('冲突发生在全部打卡提交之后（刷新统计时）', [('refresh', 1)]),
    ('冲突发生在最后一次提交时', [('commit', 4)]),
    ('冲突发生在更新设备记录时', [('device', 1)]),
]:
    state, error = store(FakeDB(conflicts))
    check(name + ' → 自动重试，结果与没有冲突时完全相同', (summary(state), error), (GOOD, None))

db = FakeDB([('refresh', 1), ('refresh', 2)])
state, error = store(db)
check('连续冲突两次 → 报错', type(error).__name__, 'SerializationFailure')
check('连续冲突两次 → 已提交的打卡和待重算请求都在',
      (len(state['punches']), sorted(state['requests'])), (4, EXPECTED_REQUESTS))

db = FakeDB()
store(db)
state, error = store(db, log_id=8)
check('再次同步同样的数据 → 全部算重复，不新增、不重复登记重算',
      (summary(state, log_id=8), error), (([5, 5, 0, 0, 0, 0], 4, EXPECTED_REQUESTS, 1), None))


# ---- _run_sync 的流程：出错也要回滚后解锁 ----
def run_sync(fetch_error=None, finish_conflicts=0):
    events = []
    left = {'n': finish_conflicts}
    dev = types.SimpleNamespace(name='考勤机')
    dev.ensure_one = lambda: None
    dev.sudo = lambda: dev
    dev._try_lock = lambda: events.append('lock') or True
    dev._unlock = lambda: events.append('unlock')
    dev._commit = lambda: events.append('commit')
    dev.env = types.SimpleNamespace(cr=types.SimpleNamespace(rollback=lambda: events.append('rollback')))
    dev._retry_on_conflict = lambda func, *args: Device._retry_on_conflict(dev, func, *args)
    dev._start_sync = lambda trigger: events.append('start') or 'LOG'

    def fetch():
        events.append('fetch')
        if fetch_error:
            raise fetch_error
        return 'SNAPSHOT', None

    def finish(log, started, error):
        events.append('finish:' + (type(error).__name__ if error else 'ok'))
        if left['n']:
            left['n'] -= 1
            raise Conflict('could not serialize access due to concurrent update')

    dev._fetch_from_device = fetch
    dev._store_snapshot = lambda log, snapshot, time_result: events.append('store')
    dev._finish_sync = finish
    try:
        Device._run_sync(dev)
    except Conflict as e:
        return events, e
    return events, None


events, error = run_sync()
check('正常流程', (events, error), ([
    'lock', 'start', 'commit', 'fetch', 'commit', 'store', 'commit', 'finish:ok', 'commit',
    'rollback', 'unlock'], None))
events, error = run_sync(fetch_error=connector.ZkConnectorError('无法连接设备'))
check('连接失败 → 不入库，记录失败原因，回滚后解锁',
      ('store' in events, events[-4:], error),
      (False, ['finish:ZkConnectorError', 'commit', 'rollback', 'unlock'], None))
events, error = run_sync(finish_conflicts=1)
check('写入同步结果时冲突一次 → 重试成功', (events.count('finish:ok'), events[-2:], error), (2, ['rollback', 'unlock'], None))
events, error = run_sync(finish_conflicts=2)
check('写入同步结果时连续冲突两次 → 报错，但仍会解锁', (events[-2:], type(error).__name__),
      (['rollback', 'unlock'], 'SerializationFailure'))


# ---- 写入同步结果：提示文字、最后成功时间、失败提醒 ----
def finish_with(error, last_success_hours=1, alert_sent=False, auto_sync=True, alert_hours=6):
    written, posts = {}, []
    dev = types.SimpleNamespace(
        name='考勤机', sync_interval=5, auto_sync=auto_sync, alert_hours=alert_hours, alert_sent=alert_sent,
        last_success_at=NOW - timedelta(hours=last_success_hours), create_date=NOW - timedelta(days=30),
        write=lambda vals: written.update(device=vals),
        message_post=lambda body, **kwargs: posts.append(body))
    log = types.SimpleNamespace(message=False, new_count=3, write=lambda vals: written.update(log=vals))
    Device._finish_sync(dev, log, time.monotonic(), error)
    return written, posts


w, posts = finish_with(None)
check('成功 → 日志完成，设备空闲，记下最后成功时间',
      (w['log']['state'], w['device']['sync_status'], w['device']['last_sync_message'],
       w['device']['last_success_at'], posts), ('done', 'idle', '新增 3 条记录', NOW, []))
w, posts = finish_with(Conflict('could not serialize access due to concurrent update'))
check('重试后仍冲突 → 中文说明，而不是 SerializationFailure(...)',
      (w['log']['state'], '并发冲突' in w['log']['message'], w['device']['sync_status']), ('failed', True, 'idle'))
w, posts = finish_with(connector.ZkConnectorError('无法连接设备 192.168.1.201:4370'))
check('连接失败 → 显示原因；1 小时内不提醒', (w['device']['last_sync_message'], posts),
      ('无法连接设备 192.168.1.201:4370', []))
w, posts = finish_with(connector.ZkConnectorError('无法连接设备'), last_success_hours=7)
check('超过 6 小时没有成功 → 发一次提醒', (len(posts), '超过 6 小时' in posts[0], w['device'].get('alert_sent')),
      (1, True, True))
w, posts = finish_with(connector.ZkConnectorError('无法连接设备'), last_success_hours=8, alert_sent=True)
check('已经提醒过 → 不重复提醒', posts, [])
w, posts = finish_with(connector.ZkConnectorError('无法连接设备'), last_success_hours=8, auto_sync=False)
check('没开自动同步 → 不提醒', posts, [])
w, posts = finish_with(None, alert_sent=True)
check('恢复同步 → 发恢复通知并清除提醒标记', (len(posts), '已恢复' in posts[0], w['device'].get('alert_sent')),
      (1, True, False))

# ---- 连接器：最后读取设备信息，设备时间差不受下载时长影响 ----
order = []
conn = connector.PyzkPullConnector('192.0.2.1')
conn.get_users = lambda: order.append('users') or []
conn.get_punches = lambda: order.append('punches') or []
conn.get_info = lambda: order.append('info') or connector.DeviceInfo()
conn.snapshot()
check('下载顺序：用户 → 打卡 → 设备信息', order, ['users', 'punches', 'info'])

print('\n失败数:', failures)
sys.exit(1 if failures else 0)
