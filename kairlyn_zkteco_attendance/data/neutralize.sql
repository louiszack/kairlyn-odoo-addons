-- 复制并中和数据库（例如建立测试库）时由 Odoo 自动执行：
-- 停用考勤机的自动同步和自动校准时间，避免测试库连接或修改考勤机。
UPDATE kairlyn_zk_device
   SET auto_sync = false,
       auto_sync_time = false;
