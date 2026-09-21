# 十分钟读懂并运行第一个工作流

[English](FIRST_STEPS.en.md) · [源码阅读路线](CODE_MAP.zh-CN.md) · [SDK 参考](SDK_GUIDE.zh-CN.md)

这个例子只有两个普通 Python 函数：去掉文字两端的空白，然后统计单词数。不需要模型、API Key、浏览器或正在运行的服务。

## 1. 运行

准备 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，在仓库根目录执行。首次安装依赖需要联网，之后这个例子可以离线运行。

```sh
uv sync --locked
uv run python examples/getting_started/first_steps.py
```

输出为：

```text
{'text': 'Hello EasyAgent', 'words': 2}
```

打开[完整源码](../examples/getting_started/first_steps.py)，从下向上找 `main()`：

1. `Sequential(clean, count_words)` 连接两个节点，前一个输出成为后一个输入。
2. `Runtime(...)` 管理运行器与本地数据库的生命周期，退出 `with` 时关闭。
3. `runtime.run(...)` 执行流程，`result.value` 是最后一步的结果。
4. `workflow.export(...)` 保存图定义，便于检查节点、依赖和输入引用；导出不会再次执行业务。

函数上的 `@node` 把普通 Python 函数声明为节点。函数体仍然是普通 Python，可以写条件、循环和辅助计算。

## 2. 修改并验证

将调用中的 `"  Hello EasyAgent  "` 改成 `"  Learn workflows step by step  "`，再次运行，结果应为 `words: 5`。

运行历史保存在 `.eah/first-steps.db`，导出的图在 `.eah/first-steps.workflow.json`。图引用本机注册的 Python 函数，不嵌入函数源码；跨机器分享代码需要扩展包，不能只复制这个 JSON。

## 3. 只记住这些边界

- **节点**：一次输入、一次输出的独立操作。同一函数中的计算整体执行和重试。
- **工作流**：节点及其依赖关系。这个例子中是先清理，再统计。
- **Agent**：需要模型判断、选择工具或反复尝试时使用的一种节点。本例的确定性计算不需要 Agent。
- **Runtime / Hub**：`Runtime` 是写脚本的入口，内部使用 `Hub` 执行和保存任务；不是两套执行引擎。

下一步按目标选择：需要多输入、并行和模型服务，读[搜索到生图](GETTING_STARTED.zh-CN.md)；准备修改框架，读[源码路线](CODE_MAP.zh-CN.md)；查询具体参数，读[SDK 参考](SDK_GUIDE.zh-CN.md)。
