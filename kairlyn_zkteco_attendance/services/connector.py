"""与考勤机通讯的连接层。

上层（设备模型）只依赖这里定义的数据结构，不直接接触 pyzk。
以后接入其他协议（例如 ADMS 推送）的设备时，新增一个连接器类即可。
设备端只读为主：不删除记录、不修改用户；唯一的写操作是校准设备时间（set_time）。
"""
from dataclasses import dataclass, field
from datetime import datetime
import logging
import re

from ..lib.zk import ZK

_logger = logging.getLogger(__name__)

# 姓名解码失败或为空时，pyzk 会填入 "NN-<编号>" 作为占位
_PLACEHOLDER_NAME = re.compile(r'^NN-\S*$')


class ZkConnectorError(Exception):
    """连接或读取设备失败。"""


@dataclass
class DeviceInfo:
    serial_number: str = ''
    firmware_version: str = ''
    platform: str = ''
    device_name: str = ''
    device_time: datetime | None = None  # 设备本地时间（无时区）
    user_count: int = 0
    record_count: int = 0
    record_capacity: int = 0


@dataclass
class DeviceUser:
    user_code: str          # 设备上的用户编号（考勤号码）
    name: str = ''
    device_uid: int = 0     # 设备内部序号
    privilege: int = 0


@dataclass
class DevicePunch:
    user_code: str
    local_time: datetime    # 设备本地时间（无时区）
    verify_type: int = 0
    punch_state: int = 0
    device_uid: int = 0


@dataclass
class DeviceSnapshot:
    info: DeviceInfo
    users: list = field(default_factory=list)
    punches: list = field(default_factory=list)


class PyzkPullConnector:
    """通过 TCP/UDP 4370 端口（ZK 标准 SDK 协议）主动拉取数据。"""

    def __init__(self, ip, port=4370, password=0, timeout=30, force_udp=False, encoding='UTF-8'):
        self.ip = ip
        self.port = port
        self.password = password
        self.timeout = timeout
        self.force_udp = force_udp
        self.encoding = encoding
        self._conn = None

    def __enter__(self):
        zk = ZK(
            self.ip,
            port=self.port,
            timeout=self.timeout,
            password=self.password,
            force_udp=self.force_udp,
            ommit_ping=True,  # 容器内通常没有 ping 命令
            encoding=self.encoding,
        )
        try:
            self._conn = zk.connect()
        except Exception as e:
            raise ZkConnectorError('无法连接设备 %s:%s：%s' % (self.ip, self.port, e)) from e
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._conn:
            try:
                self._conn.disconnect()
            except Exception:
                _logger.warning('断开设备 %s 连接时出错', self.ip, exc_info=True)
            self._conn = None
        return False

    @staticmethod
    def _safe(func, default=''):
        try:
            value = func()
        except Exception:
            _logger.debug('读取设备属性失败', exc_info=True)
            return default
        return default if value is None else value

    def get_info(self):
        conn = self._conn
        try:
            conn.read_sizes()
        except Exception as e:
            raise ZkConnectorError('读取设备容量信息失败：%s' % e) from e
        return DeviceInfo(
            serial_number=str(self._safe(conn.get_serialnumber)).strip(),
            firmware_version=str(self._safe(conn.get_firmware_version)).strip(),
            platform=str(self._safe(conn.get_platform)).strip(),
            device_name=str(self._safe(conn.get_device_name)).strip(),
            device_time=self._safe(conn.get_time, None),
            user_count=getattr(conn, 'users', 0) or 0,
            record_count=getattr(conn, 'records', 0) or 0,
            record_capacity=getattr(conn, 'rec_cap', 0) or 0,
        )

    def get_users(self):
        try:
            users = self._conn.get_users()
        except Exception as e:
            raise ZkConnectorError('读取设备用户失败：%s' % e) from e
        result = []
        for u in users:
            code = str(u.user_id or '').strip() or str(u.uid)
            # 不读取密码、卡号、生物特征
            result.append(DeviceUser(
                user_code=code,
                name='' if _PLACEHOLDER_NAME.match((u.name or '').strip()) else (u.name or '').strip(),
                device_uid=u.uid or 0,
                privilege=u.privilege or 0,
            ))
        return result

    def get_punches(self):
        try:
            records = self._conn.get_attendance()
        except Exception as e:
            raise ZkConnectorError('读取考勤记录失败：%s' % e) from e
        return [
            DevicePunch(
                user_code=str(r.user_id or '').strip() or str(r.uid),
                local_time=r.timestamp,
                verify_type=r.status or 0,
                punch_state=r.punch or 0,
                device_uid=int(r.uid) if str(r.uid).isdigit() else 0,
            )
            for r in records
        ]

    def get_time(self):
        """读取设备当前时间（设备本地时间，无时区）。"""
        try:
            return self._conn.get_time()
        except Exception as e:
            raise ZkConnectorError('读取设备时间失败：%s' % e) from e

    def set_time(self, local_time):
        """设置设备时间（设备本地时间，无时区）。这是本模块唯一会写入设备的操作。"""
        try:
            self._conn.set_time(local_time)
        except Exception as e:
            raise ZkConnectorError('设置设备时间失败：%s' % e) from e

    def snapshot(self):
        users = self.get_users()
        punches = self.get_punches()
        # 下载记录可能要几十秒，最后再读设备信息，算出的设备时间差才准确
        info = self.get_info()
        return DeviceSnapshot(info=info, users=users, punches=punches)
