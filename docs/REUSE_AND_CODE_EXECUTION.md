# 组件复用与代码扩展

本文件区分已交付的复用能力与接下来需要实现的代码开发运行环境。框架不能依靠提前穷举业务原子操作来覆盖所有需求。

## 已实现：跨工作流复用

同一 Hub 中，HTTP 节点通过 `target` + `tool_revision` 复用；Python/JS/Rust 插件通过工具名复用。HTTP 定义已版本化，新扩展协议提供不可变源码包、权限、签名与发布机制。

保存的工作流可作为子流程组件引用，无需复制并维护另一份流程体：

```json
{
  "name": "旅行项目",
  "inputs": {"message": "预订机票和酒店"},
  "steps": [{
    "id": "classify",
    "kind": "subworkflow",
    "workflow_ref": {"id": "life.classification", "revision": 2},
    "input": {"message": {"$ref": "$input.message"}}
  }]
}
```

`workflow_ref.id` 必须来自已保存的流程。省略 revision 时，保存或提交时解析最新版本并固定下来；运行途中不追随升级。准备阶段展开并保存完整 body 快照，检查所有传递依赖，限制嵌套深度。子运行继续沿用预算、审批、等待输入和恢复机制。foreach 也可引用组件；需要把 item 映射为其他参数时，在其 body 中加入一个引用式 subworkflow。

Studio 已保存列表提供“用作子流程”：创建一个独立的外层工作流，引用所选版本并映射初始输入。高级设置可更换所引用版本，或转成独立副本再编辑。画布显示组件 ID 和版本。

Agent 的开发授权可通过 `development.workflows: {"library.component": 1}` 明确授予其他命名空间组件的读取和使用权。组件内所需工具、模型仍须在调用者授权内；读取共享组件不自动扩张权限。Agent 没有共享组件的修改权，只有自己的命名空间可以写入。生成的 wrapper 仍可保存在自己的空间供其他任务复用。

Python 数据契约、JS 类型声明、Rust 类型声明都包含 workflow_ref。Rust 同时保留 tool_revision，避免反序列化再提交时丢失固定版本。

已支持 API/确定性子流程跨 Hub 的完整依赖组件包和接收端凭证重绑，见 [组件包](COMPONENT_PACKAGES.md)。组件包不包含完整 Hub 或手机运行时；完整工作流包可以包含新协议扩展源码，不能作为无需部署运行环境的独立安装包。

## 代码扩展当前实现

Agent 可使用 `code.create`、`code.test`、`code.publish` 写纯 JavaScript/WASM 计算工具。候选包含严格 Manifest、源码、每个工具的输入/预期输出场景；执行后记录实际结果。测试失败拒绝发布，发布经审批后成为固定版本工具，可跨工作流调用并随完整工作流分享。参数 Schema 错误回传 Agent 修正。示例：`uv run --extra app python -m examples.demos.live_extension_code --live --model live-gpt`。

完整 Python/Node/Rust 扩展另由开发者打包并授予精确源码摘要信任；环境筛选与超时不是操作系统沙箱。Rust 离线使用锁定依赖，安装器不会执行任意依赖安装脚本。无需提前穷举业务原子操作；纯规则通过源码扩展，网络/文件等能力经已配置的工具服务调用。

所有入口与生命周期详见[扩展指南](EXTENSIONS.md)。JSON Schema、工具输入输出、版本、写审批和分享包格式为唯一接口来源。无 Docker；手机端共享 Rust 状态机与宿主执行子集，完整 Hub 能力与真机后台的区别见[移动端范围](MOBILE_RUNTIME_STRATEGY.md)。

尚未交付任意 Python 第三方依赖的自动隔离安装、所有手机本地动态代码运行器、自动跨设备转交与商店发行。真实模型成功案例仅证明该任务闭环，不能保证任意生成代码都正确；需要独立编写的业务验收场景。
