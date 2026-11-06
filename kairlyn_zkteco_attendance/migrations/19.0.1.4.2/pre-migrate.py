def migrate(cr, version):
    """通讯密码从数字改为文本：保留原来的密码，原来的 0（未设置）改为空。"""
    cr.execute("""
        SELECT data_type FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'kairlyn_zk_device' AND column_name = 'comm_password'
    """)
    row = cr.fetchone()
    if row and row[0] == 'integer':
        cr.execute("""
            ALTER TABLE kairlyn_zk_device
            ALTER COLUMN comm_password TYPE varchar USING NULLIF(comm_password, 0)::varchar
        """)
