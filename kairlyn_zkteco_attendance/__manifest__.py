{
    'name': 'ZKTeco 考勤同步',
    'version': '19.0.1.4.3',
    'category': 'Human Resources/Attendances',
    'summary': '从 ZKTeco 考勤机同步原始打卡记录，维护设备用户与员工映射',
    'description': """
ZKTeco 考勤同步
==============
* 设备管理：IP、端口、通讯密码、时区、导入起始时间；可选的设备时间校准
* 手动同步与定时同步，原始打卡记录自动去重
* 设备用户与 Odoo 员工映射（支持离职人员、多台设备）
* 同步日志
* 管理员专用的测试数据清理
* 每日考勤结果：按员工的工作时间表计算迟到、早退、缺卡、缺勤
* 生成 Odoo 出勤记录：签到签退齐全的日子写入出勤记录，供仪表盘、报表、加班和按出勤计薪使用

内置 pyzk（GPL-2.0，见 lib/zk/LICENSE.txt），本模块仅供内部使用，不得对外分发。
    """,
    'author': 'Kairlyn',
    'license': 'GPL-2 or any later version',
    'depends': ['hr_attendance', 'mail'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/zk_device_views.xml',
        'views/zk_device_user_views.xml',
        'views/zk_punch_views.xml',
        'views/zk_sync_log_views.xml',
        'views/zk_attendance_day_views.xml',
        'views/res_config_settings_views.xml',
        'views/hr_employee_views.xml',
        'views/hr_attendance_views.xml',
        'wizard/zk_purge_wizard_views.xml',
        'wizard/zk_recompute_wizard_views.xml',
        'wizard/zk_time_wizard_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'application': False,
}
