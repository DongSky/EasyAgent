import { toolLabel } from './ui-labels.js';
// Visual editing compiles directly to Workflow dependencies and $ref mappings.
const esc = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
  );
const clone = (value) => structuredClone(value);
const typeOf = (value) =>
  value === null
    ? 'any'
    : Array.isArray(value)
      ? 'array'
      : typeof value === 'object'
        ? 'object'
        : typeof value;
const fieldNames = {
  prompt: '要处理的内容',
  instructions: '处理要求',
  capability: '模型用途',
  namespace: '资料库',
  query: '查找内容',
  mode: '检索方式',
  name: '文件名称',
  content: '保存内容',
  media_type: '文件类型',
  items: '待处理列表',
  text: '文字',
  data: '结构化结果',
  images: '图片',
  embeddings: '向量',
  citations: '参考资料',
  results: '处理结果',
  runs: '子任务',
  value: '内容',
  schema: '填写项定义',
  action: '待确认操作',
  id: '编号',
  digest: '内容摘要',
};
const kindNames = {
  tool: '调用工具',
  model: '模型处理',
  agent: '自主处理',
  retrieve: '检索资料',
  transform: '整理数据',
  artifact: '保存结果',
  input: '补充信息',
  approval: '请你确认',
  foreach: '批量处理',
  subworkflow: '复用流程',
};
const typeLabel = (type) =>
  ({
    string: '文字',
    number: '数字',
    integer: '整数',
    boolean: '是/否',
    object: '对象',
    array: '列表',
    any: '任意',
  })[type] || type;
const typeName = (schema) =>
  Array.isArray(schema?.type)
    ? schema.type.filter((t) => t !== 'null').join('|')
    : schema?.type || 'any';

export function graphEditor({ root, read, write, tools, models, flash, upload }) {
  let view = { x: 20, y: 20, zoom: 1 },
    positions = {},
    selected = null,
    pending = null,
    activeEdge = null,
    drag = null,
    history = [],
    future = [],
    current;
  root.innerHTML = `<div class="canvas-toolbar"><span>节点画布</span><button type="button" data-action="undo">撤销</button><button type="button" data-action="redo">重做</button><button type="button" data-action="layout">自动排列</button><button type="button" data-action="fit">适应画布</button><button type="button" data-action="removeEdge" disabled>断开选中连线</button><button type="button" data-action="removeNode" disabled>删除选中节点</button><button type="button" data-action="expand">展开画布</button><span data-zoom>100%</span></div><div class="canvas-help">拖动标题移动节点 · 从输出圆点拖到输入圆点连线（也可依次点击）· 拖动空白平移 · 滚轮缩放 · 点击连线后可断开</div><div class="canvas-viewport" tabindex="0" aria-label="可交互工作流画布"><div class="canvas-stage"><svg class="canvas-wires" width="6000" height="4000" aria-label="工作流连线"></svg><div class="canvas-cards"></div></div></div><div class="canvas-selection" role="status">选择节点编辑参数，输入端口显示字段类型。</div>`;
  const viewport = root.querySelector('.canvas-viewport'),
    stage = root.querySelector('.canvas-stage'),
    svg = root.querySelector('svg'),
    cards = root.querySelector('.canvas-cards');
  const status = root.querySelector('.canvas-selection');
  const button = (name) => root.querySelector(`[data-action="${name}"]`);
  const persist = () => {
    current.metadata ||= {};
    current.metadata.editor = { positions: clone(positions), viewport: clone(view) };
  };
  const snapshot = () => {
    persist();
    return clone(current);
  };
  function record() {
    history.push(snapshot());
    history = history.slice(-40);
    future = [];
  }
  function commit(message) {
    persist();
    write(clone(current));
    if (message) {
      flash(message);
      status.textContent = message;
    }
  }
  function safe(action) {
    try {
      action();
    } catch (error) {
      flash(error.message);
    }
  }
  function schemaFor(node) {
    if (node.kind === 'subworkflow' && node.body?.metadata?.component_input_schema)
      return node.body.metadata.component_input_schema;
    if (node.kind === 'tool')
      return tools().find((t) => t.name === node.target)?.input_schema || { type: 'object' };
    const schemas = {
      model: {
        prompt: { type: 'string' },
        capability: { type: 'string', enum: ['chat', 'decision', 'image', 'embedding'] },
      },
      agent: { prompt: { type: 'string' }, instructions: { type: 'string' } },
      retrieve: {
        namespace: { type: 'string' },
        query: { type: 'string' },
        mode: { type: 'string', enum: ['lexical', 'vector', 'hybrid'] },
      },
      artifact: { name: { type: 'string' }, content: {}, media_type: { type: 'string' } },
      foreach: { items: { type: 'array' } },
      input: { prompt: { type: 'string' } },
    };
    return { type: 'object', properties: schemas[node.kind] || {} };
  }
  function inputPorts(node) {
    const schema = schemaFor(node);
    return Object.entries({
      ...Object.fromEntries(
        Object.entries(node.input || {})
          .filter(([, v]) => v !== undefined)
          .map(([k, v]) => [k, typeof v === 'object' && v?.$ref ? {} : { type: typeOf(v) }])
      ),
      ...schema.properties,
    }).map(([key, s]) => ({ key, schema: s, required: (schema.required || []).includes(key) }));
  }
  function outputPorts(node) {
    let schema = tools().find((t) => t.name === node.target)?.output_schema || { type: 'object' };
    if (node.kind === 'input') schema = node.input.schema || schema;
    if (node.kind === 'transform' || node.target === 'core.echo')
      schema = {
        type: 'object',
        properties: Object.fromEntries(
          Object.entries(node.input || {}).map(([k, v]) => [k, v?.$ref ? {} : { type: typeOf(v) }])
        ),
      };
    if (['model', 'agent'].includes(node.kind))
      schema = {
        type: 'object',
        properties: {
          text: { type: 'string' },
          data: {},
          images: { type: 'array' },
          embeddings: { type: 'array' },
        },
      };
    if (node.kind === 'retrieve')
      schema = { type: 'object', properties: { citations: { type: 'array' } } };
    if (['foreach', 'subworkflow'].includes(node.kind))
      schema = {
        type: 'object',
        properties: { results: { type: 'array' }, runs: { type: 'array' } },
      };
    if (node.kind === 'artifact')
      schema = {
        type: 'object',
        properties: {
          id: { type: 'string' },
          name: { type: 'string' },
          digest: { type: 'string' },
        },
      };
    return [
      { key: '$', schema },
      ...Object.entries(schema.properties || {}).map(([key, s]) => ({ key, schema: s })),
    ];
  }
  function edges() {
    const result = [];
    for (const n of current.steps) {
      for (const dep of n.depends_on || [])
        result.push({ source: dep, target: n.id, key: '@', output: '@' });
      for (const [key, value] of Object.entries(n.input || {})) {
        if (value && typeof value === 'object' && value.$ref) {
          const [source, ...path] = value.$ref.split('.');
          if (source !== '$input')
            result.push({ source, target: n.id, key, output: path.join('.') || '$' });
        }
      }
    }
    return result;
  }
  function point(id, direction, key) {
    const card = cards.querySelector(`[data-node="${CSS.escape(id)}"]`),
      port = card?.querySelector(`[data-direction="${direction}"][data-port="${CSS.escape(key)}"]`);
    if (!port) return null;
    const r = port.getBoundingClientRect(),
      base = viewport.getBoundingClientRect();
    return {
      x: (r.x + r.width / 2 - base.x - view.x) / view.zoom,
      y: (r.y + r.height / 2 - base.y - view.y) / view.zoom,
    };
  }
  const line = (a, b) =>
    `M${a.x},${a.y} C${a.x + Math.max(65, Math.abs(b.x - a.x) / 2)},${a.y} ${b.x - Math.max(65, Math.abs(b.x - a.x) / 2)},${b.y} ${b.x},${b.y}`;
  function drawWires() {
    if (!current) return;
    svg.innerHTML = edges()
      .map((e, index) => {
        const a = point(e.source, 'out', e.output) || point(e.source, 'out', '$'),
          b = point(e.target, 'in', e.key);
        if (!a || !b) return '';
        const active = activeEdge && JSON.stringify(activeEdge) === JSON.stringify(e);
        return `<g data-edge="${index}" tabindex="0" role="button" aria-label="连线 ${esc(e.source)} 到 ${esc(e.target)} ${esc(e.key === '@' ? '执行顺序' : e.key)}"><path class="wire-hit" d="${line(a, b)}"/><path class="wire ${e.key === '@' ? 'execution' : ''} ${active ? 'selected' : ''}" d="${line(a, b)}"/></g>`;
      })
      .join('');
    if (pending) {
      const a = point(pending.node, 'out', pending.port);
      if (a && pending.cursor)
        svg.insertAdjacentHTML(
          'beforeend',
          `<path class="wire pending" d="${line(a, pending.cursor)}"/>`
        );
    }
    svg.querySelectorAll('[data-edge]').forEach((el) => {
      const select = () => {
        activeEdge = edges()[Number(el.dataset.edge)];
        selected = null;
        button('removeEdge').disabled = false;
        button('removeNode').disabled = true;
        status.textContent = `已选连线：${activeEdge.source} → ${activeEdge.target} ${activeEdge.key === '@' ? '执行顺序' : activeEdge.key}`;
        drawWires();
      };
      el.onclick = select;
      el.onkeydown = (e) => {
        if (e.key === 'Enter') select();
      };
    });
  }
  function transform() {
    viewport.scrollTop = 0;
    viewport.scrollLeft = 0;
    stage.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.zoom})`;
    root.querySelector('[data-zoom]').textContent = Math.round(view.zoom * 100) + '%';
  }
  function field(node, port) {
    const value = node.input?.[port.key],
      s = port.schema,
      type = typeName(s);
    if (value?.$ref) return `<span class="port-ref">← ${esc(value.$ref)}</span>`;
    const attrs = `${!port.required && schemaFor(node).properties?.[port.key] ? 'data-omit-empty' : ''} data-parameter="${esc(port.key)}" data-node-id="${node.id}" aria-label="${esc(node.id + ' ' + (s.title || fieldNames[port.key] || port.key))}"`;
    if (s.enum)
      return `<select ${attrs} data-enum>${!port.required ? `<option value="" ${value === undefined ? 'selected' : ''}>未设置</option>` : ''}${s.enum.map((v) => `<option value="${esc(JSON.stringify(v))}" ${v === value ? 'selected' : ''}>${esc({ chat: '文字对话', decision: '结构化决策', image: '生成图片', embedding: '生成向量', lexical: '关键词', vector: '语义', hybrid: '综合' }[v] || v)}</option>`).join('')}</select>`;
    if (type === 'boolean')
      return `<select ${attrs} data-json>${!port.required ? `<option value="" ${value === undefined ? 'selected' : ''}>未设置</option>` : ''}<option value="true" ${value === true ? 'selected' : ''}>是</option><option value="false" ${value === false ? 'selected' : ''}>否</option></select>`;
    if (['object', 'array'].includes(type))
      return (
        `<textarea ${attrs} data-json rows="2" placeholder="${type === 'array' ? '[]' : '{}'}">${value === undefined ? '' : esc(JSON.stringify(value))}</textarea>` +
        (upload
          ? Object.entries(s.properties || {})
              .filter(([, f]) => f.format === 'binary' || f.items?.format === 'binary')
              .map(
                ([key, f]) =>
                  `<span>上传 ${esc(f.title || key)}（每个文件 ≤ 2 MB）<input type="file" data-upload-node="${node.id}" data-upload-parent="${esc(port.key)}" data-upload-field="${esc(key)}" ${f.type === 'array' ? 'multiple' : ''} aria-label="${esc(node.id + ' 上传 ' + key)}"></span>`
              )
              .join('')
          : '')
      );
    return `<input ${attrs} type="${['integer', 'number'].includes(type) ? 'number' : 'text'}" ${['integer', 'number'].includes(type) ? 'data-number' : ''} value="${esc(value ?? '')}" placeholder="${port.required ? '必填' : '可选'}">`;
  }
  const expandedInputs = new Set();
  function renderInput(node, port) {
    const n = node;
    return `<div class="port-row input-row"><button class="port in" data-direction="in" data-port="${esc(port.key)}" aria-label="${n.id} 输入 ${esc(port.key)}"></button><div><label>${esc(port.schema.title || fieldNames[port.key] || port.key)}${port.required ? ' *' : ''}<small aria-hidden="true"> · ${esc(typeLabel(typeName(port.schema)))}</small>${field(n, port)}</label></div></div>`;
  }
  function renderInputs(node) {
    const ports = inputPorts(node);
    if (ports.length <= 6) return ports.map((p) => renderInput(node, p)).join('');
    const visible = ports.filter((p) => p.required || node.input?.[p.key] !== undefined),
      optional = ports.filter((p) => !visible.includes(p));
    return (
      visible.map((p) => renderInput(node, p)).join('') +
      (optional.length
        ? `<details class="graph-optional" data-optional-node="${node.id}" ${expandedInputs.has(node.id) ? 'open' : ''}><summary>更多参数 (${optional.length})</summary>${optional.map((p) => renderInput(node, p)).join('')}</details>`
        : '')
    );
  }
  function render() {
    transform();
    cards.innerHTML = current.steps
      .map((n, index) => {
        positions[n.id] ||= { x: 40 + (index % 3) * 320, y: 30 + Math.floor(index / 3) * 440 };
        const p = positions[n.id];
        return `<article class="graph-node ${selected === n.id ? 'selected' : ''}" data-node="${n.id}" style="left:${p.x}px;top:${p.y}px"><header class="graph-title" tabindex="0" aria-label="拖动节点 ${n.id}"><strong title="${esc(n.id)}">${esc(current.metadata?.step_labels?.[n.id] || (n.kind === 'tool' ? toolLabel(tools().find((t) => t.name === n.target)) : kindNames[n.kind]) || n.id)}</strong><small>${index + 1}</small></header><div class="graph-target">${esc(n.workflow_ref ? `${n.workflow_ref.id} · v${n.workflow_ref.revision ?? 'latest'}` : n.target || { input: '等待用户补充', transform: '数据映射', artifact: '保存结果', foreach: '批量子流程', subworkflow: '子流程', approval: '人工确认' }[n.kind] || n.kind)}</div><div class="execution-ports"><button class="port in execution" data-direction="in" data-port="@" aria-label="${n.id} 等待上游"></button><span>执行顺序</span><button class="port out execution" data-direction="out" data-port="@" aria-label="${n.id} 完成后执行"></button></div><div class="port-heading">输入参数</div>${renderInputs(n)}<div class="port-heading">输出数据</div>${outputPorts(
          n
        )
          .map(
            (port) =>
              `<div class="port-row output-row"><span>${esc(port.key === '$' ? '完整结果' : port.schema.title || fieldNames[port.key] || port.key)}<small aria-hidden="true"> · ${esc(typeLabel(typeName(port.schema)))}</small></span><button class="port out" data-direction="out" data-port="${esc(port.key)}" aria-label="${n.id} 输出 ${esc(port.key)}"></button></div>`
          )
          .join('')}</article>`;
      })
      .join('');
    cards.querySelectorAll('[data-optional-node]').forEach(
      (el) =>
        (el.ontoggle = () => {
          if (el.open) expandedInputs.add(el.dataset.optionalNode);
          else expandedInputs.delete(el.dataset.optionalNode);
          requestAnimationFrame(drawWires);
        })
    );
    cards.querySelectorAll('[data-parameter]').forEach((el) => {
      el.onfocus = () => record();
      el.oninput = () => {
        try {
          applyFields();
        } catch {
          /* Incomplete JSON remains editable until submission. */
        }
      };
      el.onchange = () =>
        safe(() => {
          applyFields();
          commit();
        });
    });
    cards.querySelectorAll('[data-upload-node]').forEach(
      (el) =>
        (el.onchange = async () => {
          const files = [...el.files];
          if (!files.length) return;
          const workflow = current,
            node = current.steps.find((n) => n.id === el.dataset.uploadNode);
          el.disabled = true;
          try {
            applyFields();
            const ids = [];
            for (const file of files) ids.push((await upload(file)).id);
            if (current !== workflow || !current.steps.includes(node)) {
              flash('文件已上传，请在当前流程重新选择');
              return;
            }
            record();
            node.input[el.dataset.uploadParent] ||= {};
            node.input[el.dataset.uploadParent][el.dataset.uploadField] = el.multiple
              ? ids
              : ids[0];
            commit('参考文件已加入此节点');
          } catch (error) {
            flash(error.message);
          } finally {
            el.disabled = false;
          }
        })
    );
    cards.querySelectorAll('.graph-title').forEach(
      (el) =>
        (el.onpointerdown = (e) => {
          if (e.button !== 0) return;
          e.preventDefault();
          record();
          const id = el.closest('[data-node]').dataset.node;
          selectNode(id);
          drag = { kind: 'node', id, x: e.clientX, y: e.clientY, start: clone(positions[id]) };
          el.setPointerCapture(e.pointerId);
        })
    );
    cards.querySelectorAll('.graph-node').forEach(
      (el) =>
        (el.onclick = (e) => {
          if (!e.target.closest('.port')) selectNode(el.dataset.node);
        })
    );
    cards.querySelectorAll('.port').forEach((el) => {
      el.onpointerdown = (e) => {
        e.stopPropagation();
        if (el.dataset.direction !== 'out') return;
        e.preventDefault();
        pending = { node: el.closest('[data-node]').dataset.node, port: el.dataset.port };
        status.textContent = '将此输出连接到另一个节点的输入圆点';
        drawWires();
      };
      el.onpointerup = (e) => {
        e.stopPropagation();
        if (el.dataset.direction === 'in' && pending)
          connect(
            pending.node,
            pending.port,
            el.closest('[data-node]').dataset.node,
            el.dataset.port
          );
      };
      el.onkeydown = (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          if (el.dataset.direction === 'out') {
            pending = { node: el.closest('[data-node]').dataset.node, port: el.dataset.port };
            status.textContent = '已选择输出，选择输入端口并按回车连接';
          } else if (pending)
            connect(
              pending.node,
              pending.port,
              el.closest('[data-node]').dataset.node,
              el.dataset.port
            );
        }
      };
    });
    button('undo').disabled = !history.length;
    button('redo').disabled = !future.length;
    button('removeNode').disabled = !selected;
    button('removeEdge').disabled = !activeEdge;
    requestAnimationFrame(drawWires);
  }
  function applyFields() {
    for (const el of cards.querySelectorAll('[data-parameter]')) {
      const node = current.steps.find((n) => n.id === el.dataset.nodeId);
      if (!node) continue;
      let value = el.value;
      if (value === '' && el.hasAttribute('data-omit-empty')) {
        delete node.input[el.dataset.parameter];
        continue;
      }
      if (el.hasAttribute('data-json') || el.hasAttribute('data-enum'))
        value = JSON.parse(value || 'null');
      else if (el.hasAttribute('data-number')) value = value === '' ? undefined : Number(value);
      node.input[el.dataset.parameter] = value;
    }
  }
  let flushing = false;
  function flush() {
    if (flushing || !current) return;
    flushing = true;
    try {
      applyFields();
      commit();
    } finally {
      flushing = false;
    }
  }
  function selectNode(id) {
    selected = id;
    activeEdge = null;
    cards
      .querySelectorAll('.graph-node')
      .forEach((el) => el.classList.toggle('selected', el.dataset.node === id));
    button('removeNode').disabled = false;
    button('removeEdge').disabled = true;
    status.textContent = `已选节点 ${id}。可以直接修改卡片内参数，复杂结构可在下方高级编辑中调整。`;
  }
  function cyclic(source, target) {
    const seen = new Set();
    function ancestors(id) {
      if (id === target) return true;
      if (seen.has(id)) return false;
      seen.add(id);
      return (current.steps.find((n) => n.id === id)?.depends_on || []).some(ancestors);
    }
    return source === target || ancestors(source);
  }
  function connect(source, output, target, input) {
    safe(() => {
      pending = null;
      if (cyclic(source, target))
        throw Error('不能连接：这会形成循环依赖。批量任务请使用“批量处理”步骤。');
      if ((input === '@') !== (output === '@'))
        throw Error('执行端口只能连接执行端口，数据端口用于传递参数。');
      const from = current.steps.find((n) => n.id === source),
        to = current.steps.find((n) => n.id === target);
      if (input !== '@') {
        const a = typeName(outputPorts(from).find((p) => p.key === output)?.schema),
          b = typeName(inputPorts(to).find((p) => p.key === input)?.schema);
        if (
          a !== 'any' &&
          b !== 'any' &&
          a !== b &&
          !(['number', 'integer'].includes(a) && ['number', 'integer'].includes(b))
        )
          throw Error(`字段类型不匹配：${a} → ${b}`);
      }
      record();
      to.depends_on = [...new Set([...(to.depends_on || []), source])];
      if (input !== '@') to.input[input] = { $ref: source + (output === '$' ? '' : '.' + output) };
      activeEdge = null;
      commit(`已连接 ${source} → ${target}${input === '@' ? '' : '.' + input}`);
    });
    drawWires();
  }
  function removeReferences(value, source) {
    if (Array.isArray(value)) return value.map((v) => removeReferences(v, source));
    if (value && typeof value === 'object') {
      if (value.$ref?.split('.')[0] === source) return undefined;
      return Object.fromEntries(
        Object.entries(value)
          .map(([k, v]) => [k, removeReferences(v, source)])
          .filter(([, v]) => v !== undefined)
      );
    }
    return value;
  }
  function disconnect() {
    if (!activeEdge) return;
    record();
    const e = activeEdge,
      node = current.steps.find((n) => n.id === e.target);
    if (e.key === '@') {
      node.depends_on = node.depends_on.filter((x) => x !== e.source);
      node.input = removeReferences(node.input, e.source);
      if (node.when?.source.split('.')[0] === e.source) delete node.when;
    } else {
      const source = current.steps.find((n) => n.id === e.source),
        type = typeName(outputPorts(source).find((p) => p.key === e.output)?.schema);
      node.input[e.key] =
        { string: '', number: 0, integer: 0, boolean: false, array: [], object: {} }[type] ?? null;
    }
    activeEdge = null;
    commit('连线已断开，运行前请检查必填参数');
  }
  root.querySelectorAll('[data-action]').forEach(
    (el) =>
      (el.onclick = () =>
        safe(() => {
          const action = el.dataset.action;
          if (action === 'expand') {
            root.classList.toggle('expanded');
            el.textContent = root.classList.contains('expanded') ? '收起画布' : '展开画布';
          }
          if (action === 'undo' && history.length) {
            future.push(snapshot());
            load(history.pop());
            write(snapshot());
          }
          if (action === 'redo' && future.length) {
            history.push(snapshot());
            load(future.pop());
            write(snapshot());
          }
          if (action === 'removeEdge') disconnect();
          if (action === 'removeNode' && selected) {
            record();
            const id = selected;
            current.steps = current.steps.filter((n) => n.id !== id);
            for (const n of current.steps) {
              n.depends_on = (n.depends_on || []).filter((d) => d !== id);
              n.input = removeReferences(n.input, id);
              if (n.when?.source.split('.')[0] === id) delete n.when;
            }
            delete positions[id];
            selected = null;
            commit('节点已删除');
          }
          if (action === 'layout') {
            record();
            positions = {};
            view = { x: 20, y: 20, zoom: 1 };
            commit('已自动排列');
          }
          if (action === 'fit') {
            const bounds = [...cards.querySelectorAll('[data-node]')].map((el) => ({
              x: positions[el.dataset.node].x,
              y: positions[el.dataset.node].y,
              w: el.offsetWidth,
              h: el.offsetHeight,
            }));
            if (bounds.length) {
              const left = Math.min(...bounds.map((b) => b.x)),
                top = Math.min(...bounds.map((b) => b.y)),
                right = Math.max(...bounds.map((b) => b.x + b.w)),
                bottom = Math.max(...bounds.map((b) => b.y + b.h));
              const zoom = Math.min(
                1,
                (viewport.clientWidth - 40) / (right - left),
                (viewport.clientHeight - 40) / (bottom - top)
              );
              view = { x: 20 - left * zoom, y: 20 - top * zoom, zoom };
              persist();
              transform();
              drawWires();
              write(snapshot());
            }
          }
        }))
  );
  viewport.onpointerdown = (e) => {
    if (e.target.closest('.graph-node') || e.target.closest('[data-edge]')) return;
    drag = { kind: 'pan', x: e.clientX, y: e.clientY, start: clone(view) };
    viewport.setPointerCapture(e.pointerId);
  };
  root.onpointermove = (e) => {
    if (drag?.kind === 'node') {
      positions[drag.id] = {
        x: Math.max(0, Math.min(5600, drag.start.x + (e.clientX - drag.x) / view.zoom)),
        y: Math.max(0, Math.min(3200, drag.start.y + (e.clientY - drag.y) / view.zoom)),
      };
      const node = cards.querySelector(`[data-node="${CSS.escape(drag.id)}"]`);
      node.style.left = positions[drag.id].x + 'px';
      node.style.top = positions[drag.id].y + 'px';
      drawWires();
    } else if (drag?.kind === 'pan') {
      view.x = drag.start.x + e.clientX - drag.x;
      view.y = drag.start.y + e.clientY - drag.y;
      transform();
    } else if (pending) {
      const r = viewport.getBoundingClientRect();
      pending.cursor = {
        x: (e.clientX - r.x - view.x) / view.zoom,
        y: (e.clientY - r.y - view.y) / view.zoom,
      };
      drawWires();
    }
  };
  root.onpointerup = () => {
    if (drag) {
      drag = null;
      commit();
    }
  };
  viewport.onwheel = (e) => {
    if (e.target.closest('input,textarea,select')) return;
    e.preventDefault();
    const rect = viewport.getBoundingClientRect(),
      x = e.clientX - rect.x,
      y = e.clientY - rect.y,
      zoom = Math.min(1.6, Math.max(0.25, view.zoom * (e.deltaY < 0 ? 1.1 : 0.9)));
    view.x = x - ((x - view.x) * zoom) / view.zoom;
    view.y = y - ((y - view.y) * zoom) / view.zoom;
    view.zoom = zoom;
    transform();
    persist();
    drawWires();
    write(snapshot());
  };
  viewport.onkeydown = (e) => {
    if (e.target.matches('input,textarea,select')) return;
    if (e.key === 'Escape') {
      pending = null;
      activeEdge = null;
      drawWires();
    }
    if ((e.metaKey || e.ctrlKey) && e.key === 'z') {
      e.preventDefault();
      button(e.shiftKey ? 'redo' : 'undo').click();
    }
    if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      if (activeEdge) disconnect();
      else if (selected) button('removeNode').click();
    }
  };
  new ResizeObserver(() => requestAnimationFrame(drawWires)).observe(viewport);
  function load(w) {
    current = clone(w);
    positions = clone(w.metadata?.editor?.positions || {});
    view = { x: 20, y: 20, zoom: 1, ...w.metadata?.editor?.viewport };
    selected = current.steps.some((n) => n.id === selected) ? selected : null;
    render();
  }
  return {
    render: load,
    flush,
    reset() {
      history = [];
      future = [];
      pending = null;
      activeEdge = null;
      selected = null;
    },
    read: () => read(),
  };
}
