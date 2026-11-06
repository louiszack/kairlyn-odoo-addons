def migrate(cr, version):
    """新增“最后成功同步”：用已有的同步日志填上，免得升级后第一次同步失败就立即发提醒。"""
    cr.execute("""
        UPDATE kairlyn_zk_device d
           SET last_success_at = (
                   SELECT MAX(l.end_time) FROM kairlyn_zk_sync_log l
                    WHERE l.device_id = d.id AND l.state = 'done' AND l.trigger IN ('cron', 'manual'))
         WHERE d.last_success_at IS NULL
    """)
