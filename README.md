# kairlyn-odoo-addons

Kairlyn 的 Odoo 19 自定义模块。Custom Odoo 19 addons by Kairlyn (documentation in Chinese).

## 模块

| 模块 | 说明 |
|---|---|
| [kairlyn_zkteco_attendance](kairlyn_zkteco_attendance/README.md) | ZKTeco 考勤机同步：从考勤机拉取打卡记录（TCP/UDP 4370），映射员工，按 Odoo 工作时间表计算每日考勤（迟到、早退、缺卡、旷工），生成 Odoo 出勤记录，支持补录、请假日打卡审核、锁定和校准考勤机时间 |

## 要求

- Odoo 19（社区版或企业版），依赖 `hr_attendance`、`mail`
- 考勤机：ZKTeco 标准 SDK 协议（实测 xFace600）。通讯库 pyzk 已内置，不需要另外安装

## 安装

把模块目录放进 Odoo 的 addons 路径（或把本仓库目录加进 `addons_path`），重启 Odoo，
在“应用”中更新应用列表后搜索安装。部署、升级和使用说明见各模块的 README。

## 离线测试

不需要 Odoo，也不连接考勤机：

```bash
python3 tools/test_day_logic.py    # 每日考勤判定、旷工、补录规则
python3 tools/test_sync_store.py   # 同步入库与数据库并发冲突重试
python3 tools/test_time_sync.py    # 校准考勤机时间
python3 tools/check_views.py       # 视图里的字段、按钮是否存在
python3 tools/check_code.py        # 代码中容易踩的 Odoo 坑
```

## 许可

Copyright (C) 2026 Kairlyn

本仓库的代码以 GNU 通用公共许可证第 2 版或（由你选择）任何更高版本（GPL-2.0-or-later）发布，全文见 [LICENSE](LICENSE)。
按“现状”提供，不提供任何担保。

`kairlyn_zkteco_attendance/lib/zk` 内置了 [pyzk](https://github.com/fananimi/pyzk)（GPL-2.0），版权归其作者所有。
