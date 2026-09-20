# 使用技巧（Skills）

2026-09-20。Skills 是可版本化的做事说明与参考材料，扩展负责提供实际能力。支持常见的 SKILL.md 格式；Hermes 特有的工具名称、脚本依赖和平台行为需要适配，不因导入文字就自动可用。

## 普通用户入口

打开“设置 → 使用技巧”：从搬家、旅行、学校通知、维修四个内置场景开始；也可导入 Markdown、JSON 技巧包、文件夹，或从 GitHub 浏览技巧目录。远程来源先解析到固定提交，再预览正文和文件清单，安装后生效。

“描述方法，生成技巧”把自然语言步骤整理成候选，预览后安装。生成任务有独立预算和运行记录；关闭弹窗不代表取消已经提交的生成任务。已安装技巧可查看正文、导出、停用、切换历史版本及检查来源更新。

在对话开头输入 `/meeting-action-items` 等技巧名称，选择本轮使用的做事方法。零代码构建器能发现已安装技巧。安装不会自动运行其中的脚本，也不会授予网络、记忆、终端或其他工具权限。

## 统一包格式与使用

`SkillPackage` 包含 `schema_version: 1`、`files`（相对路径 → UTF-8 文本）、`source`。必须有 `SKILL.md`，YAML 头至少包含 `name` 和 `description`。支持 `references/` 等配套文本；最多 80 个文件，单文件 128 KB，总计 1.5 MB。拒绝路径穿越、绝对路径、符号链接、远程二进制与截断的 Git tree。

Agent 两种读取方式：

- `skills: ["name"]`：把选定正文加入初始指令，并冻结配套资源。
- `skill_access: ["name"]` 配合 `tools: ["skills.read"]`：初始只放名称和说明，模型需要时读取正文或相对资源路径。

提交时冻结资源；之后更新、停用或回滚已安装技巧，不改变旧任务。`skills.read` 仅访问该 Agent 快照中的文件。`skills.list` 用于发现目录，不授予读取或执行权。完整工作流分享可携带选定资源快照，因此应先检查其中是否包含私人材料。

授权 Agent 创作时，提供 `skill_namespace` 和 `skills.save`。名称限定为授权前缀，保存走写入审批、期望版本和幂等恢复；不会自动批准自己的候选。已有“从运行复盘生成经验 → 评估 → 发布”仍是独立链路。

## API、CLI 与 SDK

- `GET /v1/skills`：可用技巧目录；`GET /v1/skill-packages`：全部已安装版本。
- `POST /v1/skill-packages/source`：`repository`、`path`、`ref`；目录留空浏览，返回固定 SHA。
- `POST /v1/skill-packages/preview`：校验包；`POST /install`：`{package, expected_revision}`。
- `GET /v1/skill-packages/{name}?revision=N`：正文与可导出包。
- `POST /v1/skill-packages/{name}/activate`：`{revision:N}` 启用/回滚，`{revision:null}` 停用。
- `POST /v1/skill-drafts`：`{requirement, model:"auto"}` 创建草稿任务；`GET /v1/skill-drafts/{id}` 返回状态、候选和预览。

CLI：`easyagent skill list`、`easyagent skill source owner/repo --path skills/example`、`easyagent skill install ./my-skill`、`easyagent skill package ./my-skill --output skill.json`。使用 `easyagent skill --help` 核对 URL/版本参数。Python、JavaScript、Rust SDK 均提供目录、安装和目标任务方法，其余接口可用通用 `request`。

## 验证与边界

集成场景覆盖生成草稿→预览→安装→按需资源读取、历史快照、更新冲突、停用/回滚、会话指令、完整工作流分享、空库重启及远程来源校验。真实 Hermes `meeting-action-items` 源文件已从固定提交读取并验证格式；这不代表其所引用的所有第三方连接器已实现。
