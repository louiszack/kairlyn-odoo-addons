"""离线测试“校准设备时间”的逻辑（不需要 Odoo，也不连接真实考勤机）。

运行：python3 tools/test_time_sync.py
用一个模拟的设备连接，验证换算方向、校准阈值、自动校准上限、手动校准和测试库保护。
"""
import importlib
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'kairlyn_zkteco_attendance')
CLOCK = {'now': datetime(2026, 10, 18, 6, 0, 0)}  # Odoo 服务器时间（UTC，无时区）


class _Any:
    def __init__(self, *args, **kwargs):
        pass


class _Datetime(_Any):
    now = staticmethod(lambda: CLOCK['now'])


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
Device = zk_device.KairlynZkDevice

ZONE = ZoneInfo('Asia/Shanghai')


class FakeConn:
    """模拟考勤机：offset 为设备时钟比真实时间快多少秒。"""

    def __init__(self, offset):
        self.offset = offset
        self.set_calls = []

    def get_time(self):
        real_local = CLOCK['now'].replace(tzinfo=timezone.utc).astimezone(ZONE).replace(tzinfo=None)
        return real_local + timedelta(seconds=self.offset)

    def set_time(self, local_time):
        self.set_calls.append(local_time)
        real_local = CLOCK['now'].replace(tzinfo=timezone.utc).astimezone(ZONE).replace(tzinfo=None)
        self.offset = int((local_time - real_local).total_seconds())


def fake_device(neutralized=False):
    dev = types.SimpleNamespace(time_sync_threshold=30, time_sync_max_auto=3600)
    dev.ensure_one = lambda: None
    dev._is_neutralized = lambda: neutralized
    dev._local_to_utc = lambda local_dt, zone: Device._local_to_utc(dev, local_dt, zone)
    return dev


failures = 0


def check(name, got, expected):
    global failures
    ok = got == expected
    failures += not ok
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else f'  got={got!r} expected={expected!r}'))


def run(offset, force=False, neutralized=False):
    conn = FakeConn(offset)
    result = Device._adjust_device_time(fake_device(neutralized), conn, ZONE, force=force)
    return result, conn


r, c = run(10)
check('相差 10 秒（小于阈值 30 秒）→ 不校准', (r['adjusted'], r['offset'], c.set_calls), (False, 10, []))
r, c = run(50)
check('相差 50 秒 → 自动校准', (r['adjusted'], r['offset'], r['after']), (True, 50, 0))
check('写入设备的是服务器时间换算成北京时间（UTC 06:00 → 14:00）', c.set_calls, [datetime(2026, 10, 18, 14, 0, 0)])
r, c = run(-120)
check('设备慢 2 分钟 → 也会校准', (r['adjusted'], r['offset'], r['after']), (True, -120, 0))
r, c = run(2 * 3600)
check('相差 2 小时（超过上限 1 小时）→ 不自动校准，只报警',
      (r['adjusted'], c.set_calls, '超过自动校准上限' in r['note']), (False, [], True))
r, c = run(2 * 3600, force=True)
check('相差 2 小时，手动校准 → 校准', (r['adjusted'], r['after']), (True, 0))
r, c = run(5, force=True)
check('相差 5 秒，手动校准 → 也会校准', (r['adjusted'], r['after']), (True, 0))
r, c = run(50, neutralized=True)
check('测试库（已中和）→ 不写入设备', (r['adjusted'], c.set_calls, '不允许' in r['note']), (False, [], True))

print('\n失败数:', failures)
sys.exit(1 if failures else 0)
