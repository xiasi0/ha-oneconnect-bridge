# 服务适配器真实验证记录

验证日期：2026-10-20
验证环境：Home Assistant 2026.10.2，OneConnect Bridge 0.1.x（`f2c7e25` 之前的旧版映射格式），
3 个父网关 / 137 个子设备在线。

> 说明：本次新增的适配器在**未升级集成**的前提下，用真实 HA 状态离线验证
> （直接调用适配器的 `read_state` / `apply_command`，数据来自 HA REST API）。
> 集成侧注册与 MQTT 回推在旧版映射格式下已由现有 137 个设备验证过；
> 新版映射格式下的端到端注册需升级后才能跑，已单独标注。

## 1. switch1（多键开关 Profile）

| 项 | 内容 |
|---|---|
| 来源单品 | `ZG2R` 智能开关 奥斯陆(无线版) 1键 |
| Profile | https://smarthome-drcn.dbankcdn.com/device/guide/ZG2R/ZG2R.json |
| 默认契约 | `switch1.on` 为 `bool`；`on_type: int`（默认）上传 0/1，`on_type: bool` 上传 JSON 布尔 |
| 真实设备 | 20 个 ZG2R 开关子设备（旧版映射下已注册并双向验证） |

验证结果：

- HA 状态 → `{"on": 0}` / `{"on": 1}`，JSON 类型为 int ✅
- `on_type: bool` → `{"on": true}` / `{"on": false}` ✅
- 华为下发 1/0 → `switch.turn_on` / `switch.turn_off` ✅
- 实体域限制 `light` / `switch` / `fan` ✅
- 离线真实数据验证：143 个 `switch.sonoff_*` 全部输出 int ✅

## 2. mode（枚举模式服务）

| 项 | 内容 |
|---|---|
| 来源单品 | `A0E0` 海尔智能空调 |
| Profile | https://smarthome-drcn.dbankcdn.com/device/guide/A0E0/A0E0.json |
| 默认契约 | `mode` 为 `enum`：1=auto、2=cool、3=heat、4=fan_only、5=dry |
| 真实设备 | 8 台 A0E0 空调子设备（旧版映射下已注册并双向验证） |

验证结果：

- HA `hvac_mode` → `{"mode": 2}`（cool），JSON 类型为 int ✅
- `heat_cool` 归并到 `auto`（enum 1）✅
- 华为下发 2 → `climate.set_hvac_mode(hvac_mode="cool")` ✅
- 华为下发 3 → `climate.set_hvac_mode(hvac_mode="heat")` ✅
- `mapping` contract 覆盖整表生效 ✅
- `select` 实体支持（`select_option`）✅
- 空调关机（`state=off`）→ `{}`：A0E0 的 `mode` enum 没有 off 值，
  关机由同 Profile 的 `switch.on` 表达，符合 Profile 语义 ✅

## 3. temperature（温度服务，十分之一度）

| 项 | 内容 |
|---|---|
| 来源单品 | `A0E0` 海尔智能空调 |
| Profile | https://smarthome-drcn.dbankcdn.com/device/guide/A0E0/A0E0.json |
| 默认契约 | `temperature.target`（int 16..30）与 `temperature.current`（int -20..50），线上单位为十分之一度；`step: 1.0` |
| 真实设备 | 8 台 A0E0 空调子设备 |

验证结果（离线真实数据，8 台空调）：

- `target=16` → `{"target": 160}`，int ✅
- `target=25` → `{"target": 250}`，int ✅
- `current=29` → `{"current": 290}`，int ✅
- 华为下发 250 → `climate.set_temperature(temperature=25.0)` ✅
- `factor: 1`（整度 Profile）→ `{"target": 25}` ✅
- `step: 0.5` → 23.4 ℃ 量化为 23.5 ℃ → `{"target": 235}` ✅
- `min/max`（摄氏度域）→ 99 ℃ 被 clamp 到 30 ℃ → `{"target": 300}` ✅
- `unavailable` → `{}` ✅

开发中抓到并修复的问题：

- `step` 最初按线上值量化而非摄氏度域，导致 `step: 0.5` 时 23.4 ℃ 输出 234 而不是 235。
  已修：`min`/`max`/`step` 统一在摄氏度域量化，最后才乘以 `factor`。

## 4. motorization（窗帘电机服务）

| 项 | 内容 |
|---|---|
| 来源单品 | `1004` 杜亚电动窗帘 DT360E |
| Profile | https://smarthome-drcn.dbankcdn.com/device/guide/1004/1004.json |
| 默认契约 | `operationMode`（W，1 开 / 2 关 / 3 停）、`targetPercent`（RW，1..100 %）、`currentPercent`（R，1..100 %） |
| 真实设备 | 4 个杜亚窗帘 + 1 个投影幕布子设备 |

验证结果（离线真实数据，9 个 cover）：

- `state=open, position=100` → `{"currentPercent": 100}`，int ✅
- `state=closed, position=0` → `{"currentPercent": 0}`，int ✅
- 华为下发 `operationMode=1/2/3` → `cover.open_cover` / `close_cover` / `stop_cover` ✅
- 华为下发 `targetPercent=50` → `cover.set_cover_position(position=50)` ✅
- `action`（OTA 触发：0 检查升级 / 1 启动升级）**故意不映射**，不驱动窗帘 ✅
- `unavailable` → `{}` ✅

开发中抓到并修复的问题：

- 默认 `min` 最初沿用 Profile 文档的 1，导致全关窗帘上报 1 而不是 0。
  已修：默认 `min: 0` 对齐 HA cover 的 0..100 语义，需要时用 `min: 1` 显式覆盖。

## 5. 已知限制（提交时如实记录）

1. **`mode` 的默认表来自 A0E0**。其他产品的 `mode` enum 集不同，必须通过
   `mapping` contract 覆盖；未覆盖的值会被丢弃而不是猜测。
2. **`temperature` 的默认 `factor: 10`** 来自 A0E0。整度 Profile 需要
   `factor: 1`。
3. **`motorization` 只覆盖 `operationMode` 与百分比**。同服务的
   `direction` / `favoriteLimit` / `version` 等特性未映射。
4. **新版映射格式下的端到端注册未验证**。旧版映射格式下这四类服务的
   注册与双向同步已由生产环境 137 个设备验证；切换到新版 `entities:` 语法后
   需要重新跑一次端到端（升级本身是破坏性变更，已与使用者确认推迟）。

## 6. 单元测试

```
tests/test_real_device_adapters.py   28 个用例
tests/test_service_adapters.py        9 个用例（上游原有）
─────────────────────────────────────────────
合计                                   37 passed
```

覆盖：四个适配器的默认契约、全部可覆盖 contract 变体
（`on_type` / `mapping` / `factor` / `min` / `max` / `step`）、
非法 service_type / contract 键 / 实体域 / 实体数量的拒绝、
`unavailable` 与缺失属性的处理、注册表暴露检查。

---

# 补充：第二批适配器（2026-10-20 追加验证）

## 5. boolean（冰箱单特性布尔服务）

来源单品 `A0VG`，Profile https://smarthome-drcn.dbankcdn.com/device/guide/A0VG/A0VG.json
覆盖 sid：`intelligenceMode` / `dailyStatus` / `travelMode` / `quickFreezingMode` / `quickRefrigeratingMode`
（五个服务的 sid、st、characteristic 三者同名，仅布尔值不同）

真实验证（5 个冰箱开关实体）：

- `dailyStatus` 开机 → `{"dailyStatus": true}` ✅
- 其余关机 → `{"<sid>": false}` ✅
- 华为下发 → `switch.turn_on` / `turn_off` ✅
- `on_value`/`off_value` contract 支持 int 编码 ✅
- `characteristic` contract 支持改名 ✅

## 6. clothesHanger（好太太晾衣机）

来源单品 `174P`，Profile https://smarthome-drcn.dbankcdn.com/device/guide/174P/174P.json

真实验证（1 台晾衣机，3 个实体）：

- `field: action` + cover → `closed` 报 `{"action": 0}`、`open` 报 `{"action": 1}` ✅
- 华为下发 0/1/2 → `cover.close_cover` / `open_cover` / `stop_cover` ✅
- `field: lighting` + light → `{"lighting": false}`，下发 → `light.turn_on` ✅
- `field: disinfect` + switch → `{"disinfect": false}` ✅
- field 与实体域不匹配时抛 `ValueError` ✅
- 同服务多绑定通过重复声明 `clothesHanger` + 不同 `field` contract 实现 ✅

## 7. enum_service（action / status，无默认表）

来源单品 `A2GQ` / `A2GT` / `2QXU`，Profile 见各适配器 docstring。

真实验证：

- `status` + A2GQ 表 → 烘干机 `finished` 报 `{"status": 2}` ✅
- `status` + A2GT 表 → 洗碗机 `unknown` 报 `{}`（未知状态丢弃）✅
- `action` + A2GT 表 → 洗碗机 `off` 报 `{"action": 1}` ✅
- 空 `mapping` 或缺失 `mapping` → `ValueError` ✅
- `status` 注册为只读（两个来源 Profile 均 `method: R`），不下发 HA 服务 ✅
- `action` 注册为可写（`method: RW`）✅

## 8. leftTime / lefttime1（剩余时间）

来源单品 `A2GQ`（`leftTime.time`）与 `A2GT`（`lefttime1.lefttime`），均为 `int` 0..1440 分钟。

真实验证：

- 烘干机 `remaining_program_time=0` → `{"time": 0}` ✅
- 洗碗机 `remaining_program_time=3` → `{"time": 3}` ✅
- 实体 state 为字符串 `"55"` 时正确解析为 `{"time": 55}` ✅
- 超出 0..1440 被 clamp ✅
- `characteristic` 默认按 sid 取自来源 Profile（`leftTime→time`、`lefttime1→lefttime`）✅

## 第二批开发中抓到并修复的问题

| 问题 | 现象 | 修法 |
| --- | --- | --- |
| `leftTime` characteristic 默认值取自 service_type | A2GT 输出 `lefttime1` 而非 `lefttime` | 增加已知 sid→characteristic 默认表 |
| `leftTime` 不接受字符串 state | HA state `"55"` 被丢弃返回 `{}` | 增加 `_as_number` 解析（HA state 永远是字符串） |
| `clothesHanger` 域校验缺失 | `validate_entities` 不校验实体域 | 拆为全域集合校验 + field 级域校验 |
| `status` 被注册为可写 | 会对只读特性误发 HA 服务 | 注册时 `writable=False` |

## 单元测试（第二批后）

```
tests/test_service_adapters.py            9
tests/test_real_device_adapters.py       28
tests/test_real_device_adapters_extra.py 24
─────────────────────────────────────────────
合计                                     61 passed
```

---

# 补充：第三批 —— 只读数值服务（2026-10-20）

## 9. readonly_number（冰箱温度，候选状态）

来源单品 `A0VG`，Profile https://smarthome-drcn.dbankcdn.com/device/guide/A0VG/A0VG.json
覆盖 sid：`refrigeratorTemperatureC`（-38..50）、`freezerTemperatureC`（-23..50）、
`vtRoomTemperature`（-38..50），均为 `int` 整度、`method: R`。

离线真实数据验证（HA 传感器）：

- `refrigeratorTemperatureC` state `"3"` → `{"refrigeratorTemperatureC": 3}` ✅
- `freezerTemperatureC` state `"-18"` → `{"freezerTemperatureC": -18}` ✅
- 字符串 state 正确解析，超范围被 clamp ✅
- `apply_command` 不调用任何 HA 服务（只读）✅
- `factor` / `min` / `max` / `characteristic` contract 变体均已单测 ✅

**⚠️ 候选状态，端到端未确认。**

旧版映射格式下，同样的 `dataSync` 报文（`{"refrigeratorTemperatureC": 3}`）发出后：

- 云端返回 `errcode: 0`（ACK 成功）
- 但华为智慧生活 App 持续显示 Profile 最小值（-38 / -23 / -38）
- 通过 huawei_smarthome 的 MQTT `deviceDataChanged` 观察，该子设备**从未**回推属性数据
- 同一网关下 `switch` / `clothesHanger` 等 RW 特性回推正常

即：**RW 特性的 dataSync 会被云端应用，R 特性不会。** 该适配器的契约实现与单测完备，
但在云端行为被解释之前，不能标记为已支持。

开发中修复：`factor: 0` 原本静默回退到 1，现改为明确抛 `ValueError`，
与「受控 contract」原则一致。

## 单元测试（第三批后）

```
tests/test_service_adapters.py            9
tests/test_real_device_adapters.py       28
tests/test_real_device_adapters_extra.py 31
─────────────────────────────────────────────
合计                                     68 passed
```

---

# 补充：第四批 —— 多实体服务与域放宽（2026-10-21）

## 10. clothesHanger 支持一服务多实体

新版解析器要求 `product.services` 里 sid 唯一，但允许一个 binding 携带多个实体
（`entities` 是列表）。据此把 `clothesHanger` 适配器改为按 HA 域分派特性：

| HA 域 | 特性 |
| --- | --- |
| `cover` | action（升降） |
| `light` | lighting（照明） |
| `switch` / `input_boolean` | disinfect（杀菌） |
| `sensor` / `number` | status |

这样一个 child 就能同时携带晾衣机的升降、照明、杀菌，不必拆成三个设备。
`fields` contract 可限定只上报部分特性，`mapping` 可覆盖 action 表。

## 11. switch / switch1 适配器放宽

新架构严格要求 sid 与实体域语义匹配，而生产 mapping 里有几类历史绑定无法通过：

| 旧 sid | 实体域 | 处理 |
| --- | --- | --- |
| `switch` | `climate` | 适配器新增 climate 支持：非 `off` 即视为开 |
| `switch` | `sensor` | 允许绑定，用 `on_states`/`off_states` 声明状态集；无控制路径 |
| `switch` | `binary_sensor` | 同上 |
| `switch` | `humidifier` | 同上 |
| `switch1` | `lock` | 适配器新增 lock 支持：`unlocked`→开，下发 `lock.unlock`/`lock.lock` |

## 12. 真实迁移验证（备用机 192.168.3.76）

从旧 free-form binding 语法迁移 111 个子设备（63 + 23 + 25）：

- 迁移脚本零告警完成
- 新版 `load_enabled_mapping` 解析三个网关全部通过
- 重启后无 `Invalid gateway mapping` 错误
- 三个网关全部在线，子设备注册成功
- 实测上报：`{'switch': {'on': 1}, 'lefttime1': {'lefttime': 0}}`（全 int）
- 实测 `ma_tong_dian_yuan`：`{'switch1': {'on': 1}}`

## 13. 迁移过程中定位的问题

**① 西门子集成抖动导致的误判**

备用机上 `homeconnect_ws` 在 `unavailable` 与有效值之间反复翻转：

```
sensor.siemens_dishwasher_power_state   14418 条历史记录
sensor.siemens_dishwasher_salt          34893 条历史记录
对照组 switch.sonoff_10027d10c8_3           1 条
对照组 sensor.ac8226756bee_refrigeratortemperaturec  11 条
```

oneconnect 只是忠实跟随上报，不是适配器缺陷。

**② 启动期间未注册 child 每 5 秒重试**

`ha_adapter` 的启动重试对尚未完成注册的 child 返回 `False` 从而 5 秒后重试，
注册完成后自动停止。属于设计行为。

**③ 两台 HA 共用网关序列号会互相抢占云端**

主用机与备用机跑同一套网关 serial 时，云端只允许一条连接，
两边会互相踢（表现为网关 connectivity 在两台之间镜像翻转）。
本次测试期间通过禁用主用机集成、只在备用机运行来解决。

## 单元测试（第四批后）

```
tests/test_service_adapters.py            9
tests/test_real_device_adapters.py       28
tests/test_real_device_adapters_extra.py 39
─────────────────────────────────────────────
合计                                     76 passed
```

---

# 补充：第五批 —— 命令链路端到端测试（2026-10-21）

新增 `tests/test_command_roundtrip.py`（31 个用例），覆盖云端命令落到 HA 实体的完整路径，
即网关收到 `device/v3/dataSync` 风格的 `{sid: {characteristic: value}}` 报文后，
各适配器应当产生的 HA 服务调用。

## 已验证的命令映射

| sid | payload | HA 调用 |
| --- | --- | --- |
| `switch` | `{"on": 1}` (light) | `light.turn_on` |
| `switch` | `{"on": 0}` (light) | `light.turn_off` |
| `switch` | `{"on": 1}` (climate) | `climate.turn_on` |
| `switch1` | `{"on": 1}` (lock) | `lock.unlock` |
| `switch1` | `{"on": 0}` (lock) | `lock.lock` |
| `mode` | `{"mode": 2}` | `climate.set_hvac_mode("cool")` |
| `temperature` | `{"target": 250}` | `climate.set_temperature(25.0)` |
| `motorization` | `{"operationMode": 1/2/3}` | `cover.open_cover` / `close_cover` / `stop_cover` |
| `motorization` | `{"targetPercent": 55}` | `cover.set_cover_position(55)` |
| `clothesHanger` | `{"action": 1}` | `cover.open_cover` |
| `clothesHanger` | `{"lighting": true}` | `light.turn_on` |
| `clothesHanger` | `{"disinfect": true}` | `switch.turn_on` |
| `boolean` | `{"<sid>": true}` | `switch.turn_on` |
| `action` | `{"action": 0}` + mapping | `switch.turn_on` |

## 只读服务不产生任何 HA 调用

`status`、`leftTime`、`lefttime1`、`refrigeratorTemperatureC`、`freezerTemperatureC`
在收到云端命令时一律不调用 HA 服务（5 个用例）。

## 畸形报文全部丢弃

空 payload、越界枚举（`{"on": 5}`、`{"mode": 99}`、`{"operationMode": 9}`）、
类型错误（`{"on": "1"}`、`{"target": "250"}`、`{"mode": "cool"}`）、
未声明的 mapping 值（`{"action": 7}`）均不触发 HA 调用（8 个用例）。

## 适配器覆盖率

用真实生产 mapping 的 18 种 sid 逐一比对注册表，**全部有适配器**：

```
switch 84 / switch1 20 / mode 8 / temperature 8 / motorization 4
brightness 1 / clothesHanger 1 / leftTime 1 / lefttime1 1 / status 1 / action 1
quickFreezingMode / quickRefrigeratingMode / intelligenceMode / dailyStatus / travelMode 各 1
refrigeratorTemperatureC / freezerTemperatureC 各 1
```

绑定的 HA 域也全部在适配器支持范围内：
`light 73 / climate 32 / switch 23 / cover 10 / sensor 7 / lock 3 / binary_sensor 2 / humidifier 1`。

## 单元测试（第五批后）

```
tests/test_service_adapters.py            9
tests/test_real_device_adapters.py       28
tests/test_real_device_adapters_extra.py 39
tests/test_command_roundtrip.py          31
─────────────────────────────────────────────
合计                                    107 passed
```

---

# 补充：第六批 —— 温度设定点（2026-10-21）

## 14. 冰箱温度问题的真正根因与解法

**旧做法（错误）**：把 A0VG 冰箱的 `refrigeratorTemperatureC` / `freezerTemperatureC`
（`method: R`）当作温度服务上报。

**A0VG Profile 的相关特性：**

| sid | characteristic | type | range | method | 含义 |
| --- | --- | --- | --- | --- | --- |
| `refrigerator` | `target` | int | 1..9 | **RW** | 冷藏室设定温度 |
| `freezer` | `target` | int | -23..-15 | **RW** | 冷冻室设定温度 |
| `vtRoom` | `target` | int | -20..5 | **RW** | 变温室设定温度 |
| `refrigeratorTemperatureC` | 同名 | int | -38..50 | R | 冷藏室**实测**温度 |
| `freezerTemperatureC` | 同名 | int | -23..50 | R | 冷冻室**实测**温度 |
| `vtRoomTemperature` | 同名 | int | -38..50 | R | 变温室**实测**温度 |

**结论**：`method: R` 的特性由设备侧上报，虚拟设备没有真实设备，云端只能显示
Profile 的边界值（-38 / -23 / -38），这解释了 App 里恒定的占位读数。
`dataSync` 对这些特性返回 `errcode: 0` 只是 ACK，不代表云端会应用。

**可用的 RW 通道是 `target`（设定温度）。** 投影改成：

```yaml
services:
  - st: temperature
    sid: refrigerator
    contract: {characteristic: target, factor: 1, min: 1, max: 9}
  - st: temperature
    sid: freezer
    contract: {characteristic: target, factor: 1, min: -23, max: -15}
bindings:
  - sid: refrigerator
    entities: [sensor.ac8226756bee_refrigeratortargettemplevel]
  - sid: freezer
    entities: [sensor.ac8226756bee_freezertargettemplevel]
```

实测上报快照（reload 后）：

```
Startup adapter snapshot child=hai_er_bing_xiang
  'refrigerator': {'target': 80}     # 8 ℃ × 10（factor=10 时的输出）
  'freezer':      {'target': -150}
```

**注意 `factor`**：A0VG 的 `target` 是整度，必须 `factor: 1`；默认 `factor: 10`
是给 A0E0 空调（十分之一度）用的。

## 15. temperature 适配器的三项增强

| 增强 | 原因 |
| --- | --- |
| 支持 `characteristic` contract | 冰箱的特性名是 `target`，与 `st: temperature` 不同名 |
| 支持 `sensor` / `number` / `input_number` 域 | 原本只接受 `climate`，无法绑定冰箱的 sensor |
| 支持 `factor: 1` | 整度 Profile（冰箱）与十分之一度 Profile（空调）共存 |

新增测试覆盖：重命名特性、整度 sensor 读取、`number.set_value` 写入路径，
以及四个域的接受与非法域拒绝。

## 16. 2026.10 版 HA 的观测限制（实测记录）

排查期间尝试过的日志与属性通道，记录以免重复踩坑：

| 通道 | 结果 |
| --- | --- |
| `ha core logs` | 只保留最后 100 行，启动期注册日志会被冲掉 |
| `docker logs` | SSH addon 保护模式开启时不可用 |
| `/api/error_log` | 该端点在该版本返回 404，已移除 |
| WebSocket `system_log/subscribe` | 订阅成功但不推送 oneconnect 的记录 |
| `huawei_smarthome_plus` | 只有 `list_devices`，无属性查询接口 |
| `config_entries/reload`（WS） | 返回 `not_found`；`unload`/`load` 返回 `unknown_command` |

可用的替代：把诊断结果写入 `.storage` 下的 JSON 文件（`store.write_json`），
或在 `hassio/addon_restart` 后立即执行一次性脚本。

## 单元测试（第六批后）

```
tests/test_service_adapters.py            9
tests/test_real_device_adapters.py       32
tests/test_real_device_adapters_extra.py 39
tests/test_command_roundtrip.py          31
─────────────────────────────────────────────
合计                                    111 passed
```

## 17. 华为云端实体的显示字符串（2026-10-21 补）

`_as_number` 原本只接受 int/float 或纯数字字符串，但华为云端把冰箱设定值渲染成
带单位的显示字符串：

```
sensor..._refrigeratortargettemplevel   '+3℃或34℉'
sensor..._freezertargettemplevel        '-18℃或0℉'
select..._vtroomtargettemplevel         '-5℃（软冷冻）或42℉'
```

这些实体的 `attributes` 里还有 `device_class: enum` 与 `options` 列表，但 `state`
本身是上面那种字符串。结果 `_as_number` 全部返回 `None`，适配器返回空 payload，
服务被静默地排除在 snapshot 之外 —— 表面现象就是 App 一直显示 Profile 边界值。

修复：`_as_number` 在直接转换失败后，取字符串中第一个带符号的整数/小数。
上述三个实体分别得到 3、-18、-5。

**端到端复验**（完整重启 HA 后）：

```
Startup adapter snapshot child=hai_er_bing_xiang state={
  'quickFreezingMode': {'quickFreezingMode': False},
  'quickRefrigeratingMode': {'quickRefrigeratingMode': False},
  'intelligenceMode': {'intelligenceMode': False},
  'dailyStatus': {'dailyStatus': True},
  'travelMode': {'travelMode': False},
  'refrigerator': {'target': 3},
  'freezer': {'target': -18},
}
```

云端实体 `sensor.ac8226756bee_refrigeratortargettemplevel` 读回 `+3℃或34℉`，
不再是 `-38` 占位值。

## 18. 操作陷阱：reload 不重载适配器代码

调试期间反复踩到的一件事：

- **`reload`（网关重载按钮）只重读 mapping.yaml，不重新导入 Python 模块。**
- 改了 `business/adapters/*.py` 之后必须**完整重启 Home Assistant**，
  否则仍是旧的适配器在跑，表现为「新 mapping 似乎没生效」。

这个现象让我一度误判适配器逻辑有问题；实际是旧模块还在内存里。
排查时可以在适配器里写一行文件输出（例如 `/tmp/xxx.jsonl`）确认代码版本，
因为 `ha core logs` 只保留最后 100 行、`/api/error_log` 在该版本已移除、
`system_log` 订阅也拿不到这些模块的日志。

## 单元测试（最终）

```
tests/test_service_adapters.py            9
tests/test_real_device_adapters.py       35
tests/test_real_device_adapters_extra.py 39
tests/test_command_roundtrip.py          31
─────────────────────────────────────────────
合计                                    113 passed
```

---

# 补充：冰箱投影的厂商白名单与四门 Profile（2026-10-21 ~ 22:05）

## 19. `devInfoSync` errcode 18002：厂商白名单

排查海尔冰箱（prodId `A0VG`，manufacturerId `371`）时，`batchBind` 成功、
`devInfoSync` 一律返回 `errcode 18002`，无论：

- 服务数量改成 1 个
- sid 与 st 改成同名
- 删除本地 identity 与 material 后重新绑定

同一网关下 7 个其他子设备全部 `devInfoSync=0, statusSync=0`，只有这台失败。

**根因是厂商**：`manufacturerId=371`（海尔）不能在网关上做虚拟子设备注册，
海尔自家 `A401`（十字门冰箱，manu 同为 371）同样返回 18002。

改用美的系 `manufacturerId=147` 的 Profile 后立刻成功：

| Profile | prodId | manu | 门型 | devInfoSync |
| --- | --- | --- | --- | --- |
| 海尔冰箱 | `A0VG` | `371` | 对开 | ❌ 18002 |
| 海尔十字门 | `A401` | `371` | 四门 | ❌ 18002 |
| 美的冰箱 | `A1KE` | `147` | 对开 | ✅ 0 |
| **COLMO 四门** | **`A1H3`** | **`147`** | **四门** | **✅ 0** |

**结论：华为云端对部分厂商不开放网关虚拟注册；必要时借用同品类、
同门型、且在白名单内的厂商 Profile。**

## 20. 最终冰箱投影（COLMO A1H3，三温区）

```yaml
product:
  prod_id: A1H3
  sub_prod_id: "00"
  device_type: "08A"
  manufacturer: "147"
  model: CRBS613AC-A8
  device_name: 冰箱
  protocol_type: 1
  setup_type: 0
  services:
    - {st: temperature, sid: refrigerator, contract: {factor: 1, min: 2, max: 8}}
    - {st: temperature, sid: freezer,      contract: {factor: 1, min: -24, max: 0}}
    - {st: temperature, sid: flexzone,     contract: {factor: 1, min: -24, max: 10}}
bindings:
  - {sid: refrigerator, entities: [sensor...refrigeratortargettemplevel]}
  - {sid: freezer,      entities: [sensor...freezertargettemplevel]}
  - {sid: flexzone,     entities: [select...vtroomtargettemplevel]}
```

**云端复验**（`GET /smart-life/v2/devices/{devId}`，即智慧生活 App 自己用的接口）：

```json
{
  "devName": "海尔冰箱",
  "devInfo": {"model": "CRBS613AC-A8"},
  "status": "online",
  "services": [
    {"sid": "refrigerator", "st": "temperature", "data": {"target": 3}},
    {"sid": "freezer",      "st": "temperature", "data": {"target": -18}},
    {"sid": "flexzone",     "st": "temperature", "data": {"target": -5}}
  ]
}
```

三个温区都有真实数据，设备为 online。这是本项目首次让云端 `services`
非空——在此之前该字段恒为 `[]`，App 因此只能显示 Profile 边界值。

## 21. 为什么必须走 App 自己的接口复核

`huawei_smarthome_plus.list_devices` 只返回设备清单（`online`、型号等），
不含特性值，无法判断上报是否落地。真正能回答「云端存了什么」的接口是
智慧生活 App 自己用的：

```
POST /smart-life/v5/devices/dynamic/data      # 批量实时值，body 为 devId 数组
GET  /smart-life/v2/devices/{dev_id}          # 单设备详情，含 services[].data
```

凭据取自 `.storage/huawei_smarthome/accounts/<账号>.json` 的
`hms_access_token` / `user_id` / `base_url`，请求头需带
`X-AppId: com.huawei.smarthome-ios`、`X-Huid`、`X-HomeId` 等。
**这是判断「App 显示是否正确」的唯一可靠依据。**

## 22. 本轮同时修复的适配器问题

| 问题 | 修法 |
| --- | --- |
| `temperature` 只接受 `st: temperature` | 同时接受 `heat`（`A401` 的温区服务用 st=heat） |
| `temperature` 只接受 `climate` 域 | 增加 `sensor` / `number` / `input_number` / `select` |
| `refrigerator`/`freezer`/`flexzone` 未注册 | 全部映射到 `temperature` 适配器 |
| `select` 域无法写入 | 按解析出的度数匹配 `options` 项后 `select_option` |
