"""OneConnect Bridge 常量定义."""

from __future__ import annotations

DOMAIN = "oneconnect_bridge"

# 每网关条目统一转发/卸载的平台
PLATFORMS: list[str] = ["binary_sensor", "sensor", "button"]

# 每网关子设备绑定文档（配置界面管理）
MAPPING_FILENAME = "mapping.yaml"

CONF_INTERFACE_IP = "interface_ip"
CONF_MAC = "mac"
CONF_SERIAL = "serial"

VIRTUAL_HARDWARE_VERSION = "1.0.0"

DEFAULT_PRODUCT_ID = "ZG37"
DEFAULT_DEVICE_TYPE = "A17"
# 华为云厂商 ID（线级 devInfo 载荷用）
DEFAULT_MANUFACTURER = "002"
# 网关型号身份固定，勿从 profile 推导显示信息
DEFAULT_MODEL = "HA-SCG-ZG37"
DEFAULT_PROTOCOL_TYPE = 1

GATEWAY_NAME = "智控网关"
GATEWAY_MANUFACTURER = "华为"
GATEWAY_DEVICE_TYPE_NAME = "蓝牙网关"

# GatewayRuntime.status 取值：gateway.py 生产、集成层消费，双方共用一套词汇
GATEWAY_STATUS_OFFLINE = "offline"
GATEWAY_STATUS_PAIRING = "pairing"
GATEWAY_STATUS_ERROR = "error"
GATEWAY_STATUS_REGISTERING = "registering"
GATEWAY_STATUS_ONLINE = "online"

DISCOVERY_PORT = 5683
DISCOVERY_MULTICAST = "238.238.238.238"
PIN_TTL_SECONDS = 300

# 防滥用上限（上游实锤值）：HA 进程内网关实例数、单网关云子设备数
MAX_GATEWAY_INSTANCES = 3
MAX_CHILD_DEVICES_PER_GATEWAY = 64

