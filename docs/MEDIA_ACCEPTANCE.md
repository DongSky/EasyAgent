# 真实多媒体验收 · 2026-09-20

## 结果

**图片和参考图动画均已成功。** 目标图片和目标动画通过 EasyAgent 的原生 API 节点、工作流审批和产物存储执行，未替换指定模型。视频补测另包含直接 HTTP 对照请求，下面分别记录。

- `gpt-image-2.5-flare`：参考图编辑，1 张、1024×1024、medium，约 55.6 秒；原始 PNG 为 1,043,444 字节。运行编号仅保留在本地验收记录中。
- `doubao-seedance-2-5-260628` 首轮测试：用生成的 PNG 作为 first_frame，1:1、4 秒、请求无音频；两次提交均返回 HTTP 503，消息为 “The model service is temporarily unavailable. Please try again later.”，没有返回任务 ID。这两次没有生成视频，失败记录保留在本地。
- 视频补测成功：将同一张表情图上传为 HTTPS 参考地址，使用 `ratio="adaptive"`，完成提交、轮询和下载。视频 720×720、约 4.06 秒、1,655,282 字节，保存在本地 `output/media/aventurine-waveflair/hosted-animation.mp4`。
- 真实调用沿用用户指定的兼容服务地址和服务端 `OPENAI_API_KEY`，未输出密钥。模型名称按目录原样使用；目录声明不等于独立核验上游模型身份。

### 官方文档复核（2026-09-20）

此前接口来自兼容服务发布的 Seedance 协议定义，并非逐项对照 BytePlus 官方教程接入。复核[Seedance 2.5 官方教程](https://docs.byteplus.com/en/docs/ModelArk/2607688)后，确认创建/查询路径为 `/api/v3/contents/generations/tasks` 和 `/api/v3/contents/generations/tasks/{id}`，鉴权为 Bearer；官方模型名为 `dreamina-seedance-2-5-260628`，本次按用户指定使用 `doubao-seedance-2-5-260628`，不能据此证明两者在兼容服务内的实际映射。

发现历史请求的一处参数不符：Seedance 2.5 的首帧/首尾帧生视频要求 `ratio="adaptive"`，会跟随首帧图片比例；此前传入了 `"1:1"`。可复用示例已改为 `adaptive`，本地 HTTP 集成增加了该约束与 Bearer 鉴权检查；4 秒仍在文档的 `[4, 30]` 范围内。该次文档复核未重新提交生成；随后按用户要求执行的真实补测见下一节。原失败请求和回执不追溯改写。

### 视频接口补测与修正（2026-09-20）

此次沿用已生成的砂金表情 PNG，没有再次调用生图模型。先修正比例，再分别检查最小文字请求、PNG/JPEG 内联图片、直接 HTTP 和框架请求、HTTPS 图片地址。

- 文字请求成功创建任务，经框架轮询、下载得到约 4 秒的 1280×720 视频。它是通用 Q 版角色的对照视频，不作为砂金参考图动画的结果。
- 当前服务对 PNG 和压缩 JPEG 的 data URL 请求持续返回 503；直接 HTTP 调用也失败。官方示例 HTTPS 图片地址则成功创建任务并完成生成。这个结果说明本次兼容服务的图片输入方式存在差异，不代表官方 Seedance API 不支持 Base64。
- 使用已提供的图片上传接口，把原始 PNG 转为 HTTPS 地址后，目标动画生成成功。框架依次执行上传、提交、等待和保存；远端任务号与各步运行编号仅保留在本地。
- 真实查询观察到初始状态 `pending`。等待节点此前仅接受 `queued/running`，会过早报错；已增加 `pending`。等待节点只重复查询，不重新提交生成。
- HTTP 事件新增脱敏的请求追踪 ID、错误码、远端任务状态与错误摘要。未知状态仍会停止，并保留实际状态，便于继续诊断。
- 服务没有遵守本次 `generate_audio=false`：最终回执为 `true`，ffprobe 也检测到 AAC 音轨。视频生成成功不等于所有可选参数都正确兼容；如需静音，应单独后处理或由服务端修正。

产物逐秒截图确认金发、额头墨镜、青绿色服饰和白色背景保留，眨眼动作随时间变化。原始视频保留其音轨，没有将后处理结果冒充原始 API 输出。

证据目录为 `.eah/live-acceptance/seedance-debug-20260920/`。其中 `control-*` 是文字对照，`image-url-*` / `url-*` 是官方示例图片对照，`image-hosted-*` / `hosted-*` 是目标表情图动画。失败请求和对应运行均单独保留。

已在本地保存可复用子工作流「图片生成动画 · 上传图片并等待视频」，并登记为 `library.seedance.image_animation`。输入是参考图片文件 ID、动作描述和时长，输出是生成回执与临时视频地址；不会固定使用本次角色。包含本机服务地址的原始分享包仅保留在忽略的 `output/` 中，不随仓库分发；新工作区现在可直接在能力库连接[默认媒体节点](DEFAULT_MEDIA_NODES.md)，也可用[媒体示例](../examples/getting_started/media/README.md)构建完整组合。示例调用的上传接口返回 `result.Resp.img_url`，其他服务可配置自己的上传操作、文件字段和返回地址路径。

以下原始产物与提示词仅保留在本地 `output/`，不随仓库分发：

- `output/media/aventurine-waveflair/chibi-expression.png`
- `output/media/aventurine-waveflair/character-reference.jpg`
- `output/media/aventurine-waveflair/reference-board.jpg`
- `output/media/aventurine-waveflair/image-prompt.txt`
- `output/media/aventurine-waveflair/video-prompt.txt`
- `output/media/aventurine-waveflair/studio-image-preview.png`

教程使用去除元数据的[生成图展示副本](assets/media/chibi-expression.png)和[公开角色参考图](../examples/assets/character-reference.jpg)。用户附件、组合参考板、界面截图、原始回执及临时下载链接不进入 Git；展示副本的文件大小可与原始产物不同。

## 角色与参考图来源

实际使用已配置的 TinyFish 搜索节点检索近期《崩坏：星穹铁道》角色，选取 **砂金·戏浪（Aventurine • Waveflair）**。官方首页当前版本信息与角色预览相互印证；称为近期新角色，不声称完整核验所有已公布、未上线角色的先后排序。

- [官方中文首页](https://sr.mihoyo.com/)，当前 4.5 版本信息。
- [官方繁体中文首页](https://hsr.hoyoverse.com/zh-tw/home)。
- [角色预览转载页](https://forum.gamer.com.tw/C.php?bsn=72822&snA=13612)，用于定位完整形象。
- [HoYoLAB CDN 角色参考图](https://upload-os-bbs.hoyolab.com/upload/community/2026/09/03/43a16b1a4d1d2eede24f3210f6e4c151_3226183336195054072.jpg)。

参考板左侧指定人物身份，右侧为用户附件的画风参考。提示词明确保留金发、额头墨镜、青紫眼睛、夏日衬衫和饰品；不复制画风参考的蓝发及女仆服。生成图人工查看后确认了这些特征，以及 Q 版大头、腮红、眨眼、V 手势和无文字的白色背景。

搜索与原始回执在本机 `.eah/live-acceptance/media-20260920/`：`search-run.json`、`character-search.json`、`reference-sources.json`、`image-run.json`、`image-success-events.json`、`video-submit-run.json`、`video-attempt1-events.json`、`video-attempt2-events.json`。

## 实测推动的修复

1. 依据具体模型匹配 Schema 分支，再识别二进制上传字段。集成测试确认服务收到图片字节，文件 ID 不会变成普通表单文本。
2. 当时新增有认证、同源与 2 MB 实际请求限制的二进制产物上传；画布支持选择参考文件。后续[对话办事](CONVERSATION_WORKSPACE.md)将原始上传接口放宽至 50 MB，画布控件仍使用 2 MB 默认值。
3. 生成图可直接用产物 ID 传入视频接口，出站时转为 data URL；流程引用无需包含大段 Base64。
4. HTTP 响应事件和脱敏错误摘要持久化。本次因此定位了公开文档中的 `format` 参数被真实服务拒绝的问题。成功请求只包含 model、image、prompt、n、size、quality，没有凭空假设另一个字段也受支持。
5. 同一接口的默认参数变更现在发布新组件版本，使节点库复用到修正后的参数。
6. 工作室增加图片预览和音视频控件。媒体结果的原始 JSON 默认收起；只渲染允许的被动媒体类型，不执行 HTML/SVG。

第一次图像失败时旧代码只保留异常类型，无法追溯精确远端原因；增加诊断后的第二次明确得到 `Unknown parameter: 'format'`（HTTP 400），第三次去掉该项后成功。前两次没有生成产物，不能把它们写成成功请求。视频失败没有自动重试循环；第二次是本轮验证中一次有限的显式重试，两次均保留了不确定写调用的核验状态。

## 验证与复现

只新增集成测试。覆盖真实本地 HTTP 的文件上传、鉴权/同源/大小边界、模型分支、错误脱敏、图片产物转视频输入、默认参数版本、完整 demo 导出与恢复。既有测试另覆盖异步等待释放 worker、重启恢复和单次提交。这些本地媒体字节是明确的协议夹具，不是模型生成结果。

首轮媒体开发的全套结果：**118 passed in 126.33s**；Ruff 与本轮三个 JavaScript 文件的语法检查通过。日志：`.eah/live-acceptance/media-20260920/final-integration-tests.log`。

Playwright 实际打开成功运行的 PNG，确认原始尺寸 1024×1024；在画布选择新的参考文件后导出，确认导出中的 ID 等于上传回执，浏览器无页面错误。证据：`ui-check.json`、`ui-export-workflow.json` 及上述工作室截图。

可复用示例：`examples/demos/multimedia_workflow.py`。运行前启动已配置 `OPENAI_API_KEY` 的 Hub，并通过 `--base-url` 指定当前兼容服务。示例保存运行 ID；重复执行同一目录会恢复已有运行，失败时停止，不自动重新扣费提交。

下面是历史验收命令，引用的是本机原始文件。新克隆请使用[搜索生图教程](GETTING_STARTED.zh-CN.md)及仓库中的公开参考图；视频示例的提示词文件需自行准备。

```sh
uv run --extra app python -m examples.demos.multimedia_workflow \
  --base-url "$OPENAI_BASE_URL" \
  --reference output/media/aventurine-waveflair/reference-board.jpg \
  --image-prompt output/media/aventurine-waveflair/image-prompt.txt \
  --video-prompt output/media/aventurine-waveflair/video-prompt.txt \
  --video-reference-mode url-upload \
  --output .eah/multimedia-demo \
  --approve-generation
```

该例从提交回执识别 `id` 或 `data.task_id`，使用包含 `pending` 的有界查询节点；成功后用独立、无生成凭证的只读节点保存 MP4，并导出含实际数据引用的工作流。`url-upload` 模式在生图与生视频之间增加上传节点，导出时保留图片到上传节点、上传地址到视频节点的数据连接。官方支持内联图片的服务可使用默认 `inline` 模式。可通过 `--video-upload-operation`、`--video-upload-file-field`、`--video-upload-url-path` 指定其他已接入的上传接口。产物引用属于当前 Hub，分享到其他实例时须重新上传输入文件。

本次视频修复后全套集成 **124 passed in 168.97s**；覆盖内联与上传 URL 两条流程、`pending` 等待、重复启动不重复生成、导出数据连接和诊断脱敏。日志 `.eah/live-acceptance/seedance-debug-20260920/full-integration.log`。
