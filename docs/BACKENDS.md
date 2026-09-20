# 服务后端与在线分发

2026-09-20。扩展页面收纳“发现与安装更多能力”和“服务后端与本机执行”，连接页面配置消息与语音服务。基础扩展开发参见 [EXTENSIONS.md](EXTENSIONS.md)。

## 专用后端契约

Manifest 的 `backends` 贡献 `{kind, handler, operations}`。宿主调用 handler 时提供 `{schema_version:1, operation, payload}`；沿用扩展进程协议、权限、限额、版本及错误处理。

- memory：search/put/remove/merge/history。search 返回 `items`（每项至少含 key、value、source）；merge 必须实现来源摘要校验与持久历史，history 返回 items。未声明的操作明确报错，不静默写入另一份本地记忆。
- context：compact，返回 summary；宿主保留原始指令和工具调用配对。
- terminal：execute，返回 exit_code/stdout/stderr。
- browser：navigate/snapshot/click/fill/close。
- approval：present，返回 receipt；展示失败不删除本地待审批记录。
- channel：send，返回 receipt。
- media：generate/transcribe/speak。内置语音实现后两者；图像/视频继续使用模型或 API 节点，或安装 generate 后端。

`GET /v1/backends` 查看契约与提供者，`POST /v1/backends/{kind}` 选择 `{extension,revision}`，DELETE 回到内置服务。绑定固定在提交时的运行快照中；被选中的扩展不能直接停用。业务记忆与框架内部配置存储独立：替换 memory 后端覆盖 Agent 和记忆管理界面的业务访问，不迁移内部状态或既有本地数据。

扩展 provider 可声明 `stream_handler` 与 `cancel_handler`。流式 handler 接收 request/cursor，返回 deltas、cursor、done 及最终 result；宿主增量发出 model.delta 并在退出时关闭游标。最多 4,096 页/4 MB；这是独立游标协议，不兼容 Pi 的 TypeScript 异步生成器接口。

## 浏览器和终端

本机执行默认关闭，配置后才进入零代码能力目录。终端使用 argv、不隐式拼 shell，限制工作目录、输出和超时，取消时终止进程树。它是受信任的宿主进程执行，并非 OS 沙箱；工作目录限制不等于进程无法访问其他文件。

浏览器使用 Playwright Chromium，限定明确的 HTTP(S) origin，禁止下载和 service worker；逐项操作进入写入审批/回执管理。每个根任务隔离，同一目标的多个修订共享会话。Cookie/localStorage 与最后 URL 加密保存，重启可恢复；未提交表单、DOM 内存和页面 JS 状态不保证恢复。

最多保留 4 个活动浏览器实例，10 分钟不用关闭；加密状态 7 天清理，显式 close 立即删除。状态不出现在用户凭证目录。桌面发行包包含驱动，浏览器二进制仍需安装 `uv run --extra app playwright install chromium`；手机通过 HTTPS Hub 使用这些能力，不依赖 Docker。

## 消息与语音

新增 Slack/Discord/飞书发送映射，沿用持久投递队列、配置版本和成功回执；无幂等能力的未知发送结果不自动重试。已有 webhook/Telegram/CalDAV 保留。入站仍使用签名通用网关，不声称每种渠道都具备原生双向适配。

语音设置包含 base_url、凭证引用、transcription_model、speech_model 和 voice。`voice.transcribe` 接收音频产物；`voice.speak` 输出可播放产物。聊天页面录音→转写填入输入框，回复可合成/播放/停止。录音最多 60 秒、上传 1 MB；支持常见 multipart transcription 和 binary speech 接口，不是实时双向语音通话。运行冻结配置，工作流分享由接收端配置自己的语音连接。

## 在线目录

管理者添加 Source（id/title/url），目录包含 items，每项声明 id/title/description/kind/url/sha256，kind 为 skill 或 extension。下载先核对摘要，再预览；安装执行既有版本、信任与权限流程。`/v1/package-sources` 支持配置与读取，`/{id}/catalog` 刷新目录，`/{id}/items/{item}` 预览包。

不自动运行来源脚本或下载 Python/npm 依赖。摘要校验不能证明来源可信，已有签名验证独立保留。没有运营中的公共市场，也不是 npm/pip 包兼容层。

## 验证范围

集成测试执行真实浏览器填表/点击、终端、JS 扩展流式后端和本地 HTTP 目录/语音/渠道协议服务，另含浏览器重建后 localStorage 恢复。真实麦克风权限、付费语音账户、消息渠道账户、所有目标操作系统及手机真机仍需环境验收。
