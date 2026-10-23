# 服务适配器贡献

服务适配器负责把华为物模型中的一个服务转换为 HA 实体状态和命令。本说明提供贡献规范和记录示例，不维护或罗列当前预置服务清单。

## 贡献原则

- 一个华为 `sid` 只能对应一个服务适配器。新增服务时使用尚未注册的 `sid`；修改已有服务时更新该 `sid` 对应的适配器，禁止重复注册或另建同义适配器。
- 同一个适配器可以服务多个 `sid`：声明 `service_ids` 元组即可，例如 `switch` / `switch1` / `switch2` / `switch3` 共用一个 `switch` 适配器，`power` / `electric` 共用一个数值映射适配器。不要为编号变体各写一份适配器——那会让同一份 Profile 语义散在多处。`registry` 在导入时会检测 `sid` 冲突并直接报错。
- `sid` 必须取自华为 Profile 的 `serviceId`，保持原样；不按 HA 平台、设备品牌或个人习惯另造名称。
- 优先贡献使用频率高、解决实际需求、可复用于多个设备的服务。适配器应清晰实现 Profile 中适用的上报和下行命令行为，而不只是声明字段。
- 代码和 PR 标题、正文、描述中不得出现任何第三方厂商名称或品牌名称；包括注释、模块说明和测试数据。说明来源时使用 `prodId` 与 Profile 链接，不写厂商名、品牌名或商品宣传名称。
- 先检查 `custom_components/oneconnect_bridge/business/adapters/registry.py`。新 `sid` 只注册一次；已有 `sid` 的差异应扩展现有适配器，并由适配器明确处理，不得以重复 `sid` 绕过差异。
- Profile 差异只有在语义兼容时才适合做成现有适配器的受控 `contract` 覆盖项。字段映射、HA 调用方式、状态读取和命令组装仍由 Python 适配器负责。
- 未经真实投影验证的行为必须标为“未验证”，不能宣称已支持。

## 贡献记录示例

提交新适配器或修改已有 `sid` 时，在 PR 描述中按以下结构记录。请替换占位内容；不要补写厂商或品牌名称。

- `sid`：华为 Profile 中的原始 `serviceId`
- `serviceType`：Profile 中的服务类型
- 来源：`prodId` 和 Profile URL
- 契约：特征名、数据类型、读写方式、范围、步长、单位及枚举
- HA 转换：绑定的实体类型、上报值转换、下行命令行为
- 验证：真实投影使用的 `prodId`、验证日期、上报与命令结果；不支持的方向或未验证项
- 价值：说明该服务解决的常见或明确需求，以及为何值得纳入

## 验收要求

按 Profile 声明的读写能力验证适用方向：

1. 使用来源单品完成子设备注册；
2. HA 状态变化后，华为侧收到正确的服务字段、值和 JSON 类型；
3. 对可写特征，验证华为侧下发命令后能正确调用目标 HA 实体；
4. 验证默认契约，以及适配器开放的每个 `contract` 覆盖项；
5. 在 PR 中记录 `sid`、来源 `prodId`、Profile URL、测试日期、验证结论和未覆盖范围，且不出现第三方厂商或品牌名称。

## 查询华为 IoT 目录和 Profile

全量目录：

`https://smarthome-drcn.dbankcdn.com/device/guide/ver_202609_hmos/mainHelp.json`

单品 Profile：

`https://smarthome-drcn.dbankcdn.com/device/guide/{prodId}/{prodId}.json`

查询流程：

1. 在全量目录中按产品名称、`DeviceId` 或型号寻找候选单品；`DeviceId` 对应 YAML 的 `prod_id`。
2. 打开单品 Profile，读取 `deviceTypeId`、`manufacturerId`、`deviceModel` 和 `protocolType`，用于填写产品身份。
3. 从 Profile 的 `services` 中读取 `serviceId` 作为 YAML `sid`，`serviceType` 作为 YAML `st`。
4. 读取对应 `characteristics` 的 `characteristicName`、`characteristicType`、`method`、`min`、`max`、`step`、`unit` 和 `enumList`，将服务契约和双向转换实现到服务适配器。
5. 如果 Profile 契约与已有服务适配器不同，先评估能否用该适配器开放的 `contract` 表达；不能表达时扩展现有适配器并完成真实投影验证。

目录和 Profile 只用于选择候选和实现契约；是否可注册、状态同步和命令链路仍以真实投影结果为准。
