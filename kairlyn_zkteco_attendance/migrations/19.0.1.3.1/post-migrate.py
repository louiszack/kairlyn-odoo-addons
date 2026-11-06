def migrate(cr, version):
    """旧版本把“待重新计算考勤的起始日期”记在员工档案上（hr_employee.zk_recompute_from），
    改为独立的待重算队列后，把尚未处理的标记转入队列。升级结束时 Odoo 会删除这一列。"""
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'hr_employee' AND column_name = 'zk_recompute_from'
    """)
    if not cr.fetchone():
        return
    cr.execute("""
        INSERT INTO kairlyn_zk_recompute_request
               (employee_id, date_from, create_uid, create_date, write_uid, write_date)
        SELECT id, zk_recompute_from, 1, NOW() AT TIME ZONE 'UTC', 1, NOW() AT TIME ZONE 'UTC'
          FROM hr_employee
         WHERE zk_recompute_from IS NOT NULL
    """)
