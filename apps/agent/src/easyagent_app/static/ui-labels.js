// Labels are presentation only. Stable API identifiers remain unchanged in saved workflows.
export const toolLabels={
  'attachments.read':'读取附件','core.echo':'原样传递内容','core.to_text':'转成文字',
  'memory.search':'查找记住的内容','memory.put':'记住一条内容','memory.remove':'移除记忆','memory.merge':'合并记忆',
  'search.tinyfish':'搜索网页','library.typesafe.evaluate':'分类与判断','decision.typesafe':'结构化判断',
  'development.catalog':'查找可用能力','development.read_document':'读取接口说明','development.save_api':'保存接口节点',
  'development.get_api':'读取接口节点','development.save_workflow':'保存可复用流程','development.get_workflow':'读取可复用流程',
  'development.run_workflow':'运行可复用流程','development.call_api':'调用接口节点',
  'agents.spawn':'启动协作助手','agents.send':'给协作助手补充信息','agents.reply':'回复协作消息','agents.status':'查看协作进度',
  'agents.wait':'等待协作结果','agents.cancel':'停止协作助手',
  'agents.parallel':'并行执行协作任务','agents.note':'记录协作发现','agents.notes':'读取协作发现','agents.broadcast':'发送协作消息',
  'files.read':'读取文件','files.write':'写入文件','files.edit':'精确编辑文件','files.find':'查找文件','files.grep':'搜索文件内容',
  'workflows.save':'保存可复用流程','workflows.run':'执行已保存流程','workflows.get':'读取流程定义','workflows.list':'查找已保存流程','workflows.schema':'查看流程与节点规范',
  'code.create':'生成代码节点','code.test':'验证代码节点','code.publish':'发布代码节点',
  'life.plan_template':'创建生活事务计划','life.capture_material':'整理生活事务材料','evidence.verify_report':'核对报告来源',
};
export const toolLabel=tool=>toolLabels[tool?.name]||(tool?.input_schema?.title)||tool?.name||'调用工具';
export const modelTypes={text:'文字',language:'语言模型',chat:'对话',image:'图片',video:'视频',audio:'音频',embedding:'向量检索',rerank:'搜索排序',llm:'语言模型',image_generation:'图片生成',video_generation:'视频生成',speech:'语音'};
