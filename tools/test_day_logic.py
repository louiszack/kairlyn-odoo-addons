"""离线测试每日考勤的判定规则（不需要 Odoo、pytz）。

运行：python3 tools/test_day_logic.py
只测试 models/zk_attendance_day.py 里的纯函数：workday_of、split_punches、evaluate_day。
"""
import importlib
import os
import sys
import types
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'kairlyn_zkteco_attendance', 'models')


# ---- 仅为能导入模块文件而准备的最小替身 ----
class _Any:
    def __init__(self, *args, **kwargs):
        pass


fields_mod = types.ModuleType('odoo.fields')
for _name in ['Many2one', 'One2many', 'Many2many', 'Date', 'Datetime', 'Char', 'Text',
              'Boolean', 'Float', 'Integer', 'Selection']:
    setattr(fields_mod, _name, _Any)
fields_mod.Command = types.SimpleNamespace(set=lambda ids: ('set', ids))
exc_mod = types.ModuleType('odoo.exceptions')
exc_mod.UserError = exc_mod.ValidationError = type('UserError', (Exception,), {})
odoo = types.ModuleType('odoo')
odoo._ = lambda text, *args, **kwargs: (text % (kwargs or args[0])) if (args or kwargs) else text
odoo.fields = fields_mod
odoo.exceptions = exc_mod
odoo.models = types.SimpleNamespace(Model=object, TransientModel=object, Constraint=_Any)
odoo.api = types.SimpleNamespace(depends=lambda *a: (lambda f: f), model=lambda f: f,
                                 model_create_multi=lambda f: f,
                                 constrains=lambda *a: (lambda f: f))
pg_errors = types.ModuleType('psycopg2.errors')
pg_errors.SerializationFailure = type('SerializationFailure', (Exception,), {})
pg_errors.DeadlockDetected = type('DeadlockDetected', (Exception,), {})
psycopg2_mod = types.ModuleType('psycopg2')
psycopg2_mod.errors = pg_errors
tools_mod = types.ModuleType('odoo.tools')
misc_mod = types.ModuleType('odoo.tools.misc')
misc_mod.clean_context = lambda ctx: {k: v for k, v in ctx.items() if not k.startswith('default_')}
sys.modules.update({'odoo.tools': tools_mod, 'odoo.tools.misc': misc_mod})
sys.modules.update({'odoo': odoo, 'odoo.fields': fields_mod, 'odoo.exceptions': exc_mod,
                    'psycopg2': psycopg2_mod, 'psycopg2.errors': pg_errors})
sys.modules['pytz'] = types.SimpleNamespace(utc=timezone.utc, timezone=ZoneInfo)
sys.modules['dateutil'] = types.ModuleType('dateutil')
sys.modules['dateutil.relativedelta'] = types.SimpleNamespace(relativedelta=_Any)
# 以包的方式导入 models 目录，模块内的相对导入才能生效
package = types.ModuleType('kzm')
package.__path__ = [MODELS_DIR]
sys.modules['kzm'] = package
m = importlib.import_module('kzm.zk_attendance_day')

TZ = ZoneInfo('America/Los_Angeles')
DAY_CHANGE = timedelta(hours=4)
DEBOUNCE = timedelta(minutes=5)
D = date(2026, 10, 14)  # 周三
UTC = timezone.utc


def t(hour, minute=0, second=0, day=D):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=TZ)


PLAN = (t(8), t(17))
LUNCH = [(t(12), t(13))]
NOON = t(12)


def ev(valid, plan=PLAN, lunches=LUNCH, flexible=False, required=True, today=False, **kwargs):
    return m.evaluate_day(sorted(valid), plan, lunches, flexible, required, today, 15, 15, NOON, **kwargs)


def hm(spans):
    return [(a.astimezone(TZ).strftime('%H:%M'), b.astimezone(TZ).strftime('%H:%M')) for a, b in spans]


failures = 0


def check(name, got, expected):
    global failures
    ok = got == expected
    failures += not ok
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else f'  got={got!r} expected={expected!r}'))


r = ev([t(7, 55), t(17, 5)])
check('正常：状态', r['state'], 'normal')
check('正常：工时 = 9小时10分 - 午休1小时', round(r['worked_hours'], 4), round(8 + 10 / 60, 4))
check('正常：迟到/早退', (r['late_minutes'], r['early_minutes']), (0, 0))
check('恰好 15 分钟不算迟到', ev([t(8, 15), t(17)])['state'], 'normal')
r = ev([t(8, 15, 30), t(17)])
check('15分30秒算迟到，记 16 分钟', (r['state'], r['late_minutes']), ('late', 16))
r = ev([t(8, 20), t(17)])
check('迟到 20 分钟（从计划时间起算）', (r['state'], r['late_minutes']), ('late', 20))
r = ev([t(8), t(16, 40)])
check('早退 20 分钟', (r['state'], r['early_minutes']), ('early', 20))
r = ev([t(8, 30), t(16, 30)])
check('迟到且早退', (r['state'], r['late_minutes'], r['early_minutes']), ('late_early', 30, 30))
r = ev([t(7, 58)])
check('只有上午一次 → 缺签退', (r['state'], r['check_in'], r['check_out']), ('missing_out', t(7, 58), None))
r = ev([t(17, 2)])
check('只有下午一次 → 缺签到', (r['state'], r['check_in'], r['check_out']), ('missing_in', None, t(17, 2)))
check('工作日无打卡 → 缺勤', ev([])['state'], 'absent')
r = ev([t(9), t(14)], plan=None)
check('休息日有打卡 → 休息日出勤', (r['state'], r['rest_day']), ('rest_work', True))
r = ev([t(8), t(17)], plan=None, lunches=[], flexible=True)
check('弹性工时：正常、不扣午休（9 小时）', (r['state'], r['worked_hours']), ('normal', 9.0))
check('不参与考勤：迟到也不标记', ev([t(9), t(17)], required=False)['state'], 'normal')
r = ev([t(8, 30), t(10)], today=True)
check('当天未结束 → 进行中，不记早退', (r['state'], r['early_minutes']), ('ongoing', 0))

# 真实数据：员工A 10/16 只在上午打了两次卡，不能把 8:26 当签退
r = ev([t(7, 57, 43), t(8, 26, 33)])
check('员工A 10/16：只有上午两次 → 缺签退，不配对', (r['state'], r['check_in'], r['check_out']),
      ('missing_out', t(7, 57, 43), None))
r = ev([t(7, 24, 20), t(8, 4, 41), t(8, 14), t(11, 30, 16)])
check('员工A 10/14：上午多次、最后 11:30 → 缺签退', (r['state'], r['check_out']), ('missing_out', None))
r = ev([t(13), t(17, 5)])
check('只有下午两次 → 缺签到', (r['state'], r['check_in'], r['check_out']), ('missing_in', None, t(17, 5)))
r = ev([t(8), t(9)], plan=None)
check('休息日上午两次 → 取首尾配对为休息日出勤', (r['state'], r['worked_hours']), ('rest_work', 1.0))
r = ev([t(13), t(21)], plan=None, lunches=[], flexible=True)
check('弹性工时：下午到晚上也能配对', (r['state'], r['worked_hours']), ('normal', 8.0))
r = ev([t(7, 58), t(12, 31)])
check('签退刚过中点 12:30 → 配对并记早退', (r['state'], r['early_minutes']), ('early', 269))

rows = [(1, t(7, 55)), (2, t(7, 57)), (3, t(12, 5)), (4, t(17, 5)),
        (5, t(3, 30, day=D + timedelta(days=1)))]  # 次日凌晨 3:30，换日前，算 D 这一天
raw, valid = m.split_punches([(i, x.astimezone(UTC)) for i, x in rows], TZ, DAY_CHANGE, DEBOUNCE)
check('凌晨 3:30 归入前一个工作日', sorted(raw), [D])
check('全部打卡都保留在当天明细里', raw[D], [1, 2, 3, 4, 5])
check('2 分钟内的重复打卡被去掉', [x.astimezone(TZ).strftime('%H:%M') for x in valid[D]],
      ['07:55', '12:05', '17:05', '03:30'])
r = ev(valid[D])
check('中间打卡忽略：首次签到、末次签退',
      (r['check_in'].astimezone(TZ).strftime('%H:%M'), r['check_out'].astimezone(TZ).strftime('%m-%d %H:%M')),
      ('07:55', '10-15 03:30'))
raw2, _ = m.split_punches([(9, t(4, 0, day=D + timedelta(days=1)).astimezone(UTC))], TZ, DAY_CHANGE, DEBOUNCE)
check('次日 4:00 整属于次日', sorted(raw2), [D + timedelta(days=1)])

rows = [(1, datetime(2026, 11, 1, 1, 30, tzinfo=TZ)), (2, datetime(2026, 11, 1, 8, 0, tzinfo=TZ))]
raw3, _ = m.split_punches([(i, x.astimezone(UTC)) for i, x in rows], TZ, DAY_CHANGE, DEBOUNCE)
check('夏令时结束日：1:30 归前一天，8:00 归当天', dict(raw3), {date(2026, 10, 31): [1], date(2026, 11, 1): [2]})

check('可生成出勤记录的状态', sorted(m.GENERATE_STATES), ['early', 'late', 'late_early', 'normal', 'rest_work'])

# ---- 休息日（含公共假日、全天请假）：取第一次和最后一次打卡，不扣午休 ----
r = ev([t(13), t(17)], plan=None, lunches=[])
check('休息日只来下午 → 休息日出勤 4 小时', (r['state'], r['worked_hours'], hm(r['intervals'])),
      ('rest_work', 4.0, [('13:00', '17:00')]))
r = ev([t(10), t(10, 20)], plan=None, lunches=[])
check('休息日进来 20 分钟 → 也配对（加班交给加班规则的容差过滤）', (r['state'], round(r['worked_hours'], 2)),
      ('rest_work', 0.33))
check('休息日只打一次卡 → 缺卡', ev([t(14)], plan=None, lunches=[])['state'], 'missing_in')
r = ev([t(8), t(12, 5), t(13, 2), t(17)], plan=None, lunches=[])
check('休息日中间打卡忽略，不扣午休 → 9 小时', (r['state'], r['worked_hours']), ('rest_work', 9.0))
r = ev([t(22), t(1, 30, day=D + timedelta(days=1))], plan=None, lunches=[])
check('周六 22:00 来、周日 01:30 走 → 休息日出勤 3.5 小时', (r['state'], r['worked_hours']), ('rest_work', 3.5))

# ---- 手工补录：以补录的签到签退为准，照常判迟到早退 ----
r = ev([t(13, 5), t(17, 30)], explicit=True, intervals=[(t(13, 5), t(17, 30))])
check('补录 13:05–17:30（没请假）→ 迟到 305 分钟，工时 4.42',
      (r['state'], r['late_minutes'], round(r['worked_hours'], 2)), ('late', 305, 4.42))
r = ev([t(8), t(17)], explicit=True, intervals=[(t(8), t(12)), (t(13), t(17))])
check('补录上午、下午两条 → 正常 8 小时', (r['state'], r['worked_hours']), ('normal', 8.0))
r = ev([t(7, 58)], explicit=True, intervals=[])
check('补录只有签到 → 缺签退', (r['state'], r['check_in']), ('missing_out', t(7, 58)))

# ---- 请假时段不算出勤 ----
r = ev([t(8), t(17)], leaves=[(t(14), t(16))])
check('请假 14:00–16:00、打卡 8:00 和 17:00 → 出勤拆成两段，工时 6 小时',
      (r['state'], hm(r['intervals']), r['worked_hours']), ('normal', [('08:00', '14:00'), ('16:00', '17:00')], 6.0))
r = ev([t(7, 55), t(17, 5)], plan=(t(13), t(17)), leaves=[(t(8), t(12))])
check('上午请假、7:55 有打卡 → 5 分钟的碎片不算', hm(r['intervals']), [('12:00', '17:05')])
check('请假时段内没有打卡 → 不需要审核',
      m.has_leave_conflict([t(8), t(17)], [(t(14), t(16))], timedelta(minutes=15), False), False)
check('请假时段内有打卡 → 需要审核',
      m.has_leave_conflict([t(8), t(15), t(17)], [(t(14), t(16))], timedelta(minutes=15), False), True)
check('请假结束前 10 分钟回来打卡（容忍内）→ 不需要审核',
      m.has_leave_conflict([t(8), t(15, 50), t(17)], [(t(14), t(16))], timedelta(minutes=15), False), False)
check('全天请假当天有打卡 → 需要审核',
      m.has_leave_conflict([t(9)], [(t(8), t(17))], timedelta(minutes=15), True), True)
check('没有请假 → 不需要审核', m.has_leave_conflict([t(9)], [], timedelta(minutes=15), True), False)

# ---- 旷工规则：迟到/早退超过 30 分钟记半天，超过 240 分钟记一天；缺勤记一天（半天班记半天）----
def absence(state, late=0, early=0, planned=8.0, half=30, full=240):
    return m.judge_absence(state, late, early, planned, 8.0, half, full)


check('迟到 20 分钟 → 不算旷工', absence('late', late=20), 0.0)
check('迟到 31 分钟 → 旷工半天', absence('late', late=31), 0.5)
check('迟到 305 分钟 → 旷工一天', absence('late', late=305), 1.0)
check('迟到 31、早退 250 → 按重的算一天', absence('late_early', late=31, early=250), 1.0)
check('工作日缺勤 → 旷工一天', absence('absent'), 1.0)
check('另半天请了假、这半天缺勤 → 旷工半天', absence('absent', planned=4.0), 0.5)
check('半天档设为 0（不启用）→ 迟到 100 分钟不算', absence('late', late=100, half=0), 0.0)
check('缺卡不算旷工（等补录）', absence('missing_out'), 0.0)

# ---- 补录规则 ----
h = sys.modules['kzm.hr_attendance']
TODAY = date(2026, 10, 20)
check('锁定日期及之前：主管不能改', h.lock_error(date(2026, 10, 25), date(2026, 10, 25), False) is not None, True)
check('锁定日期及之前：考勤管理员可以改', h.lock_error(date(2026, 10, 25), date(2026, 10, 25), True), None)
check('锁定日期之后：可以改', h.lock_error(date(2026, 10, 26), date(2026, 10, 25), False), None)
check('没选补卡原因 → 不允许', h.manual_rule_error(TODAY, TODAY, 7, 3, 0, False, True) is not None, True)
check('主管补 8 天前的（期限 7 天）→ 不允许',
      h.manual_rule_error(date(2026, 10, 12), TODAY, 7, 3, 0, 'forgot', False) is not None, True)
check('主管补 7 天前的 → 允许', h.manual_rule_error(date(2026, 10, 13), TODAY, 7, 3, 0, 'forgot', False), None)
check('本月忘记打卡已补 3 天 → 第 4 天不允许',
      h.manual_rule_error(TODAY, TODAY, 7, 3, 3, 'forgot', False) is not None, True)
check('外勤不计次数', h.manual_rule_error(TODAY, TODAY, 7, 3, 3, 'field', False), None)
check('人事不受期限和次数限制', h.manual_rule_error(date(2026, 9, 1), TODAY, 7, 3, 9, 'forgot', True), None)
check('次数设为 0（不限）', h.manual_rule_error(TODAY, TODAY, 7, 0, 9, 'forgot', False), None)

print('\n失败数:', failures)
sys.exit(1 if failures else 0)
