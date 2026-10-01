// OS notifications belong to the receiving browser, not the machine running the Hub.
const DEVICE_KEY = 'eah.notification-device.v1';
const permissionText = {
  granted: '已允许通知。系统的勿扰模式和通知设置仍可能隐藏横幅。',
  denied: '通知权限被拒绝。请在浏览器的网站设置中允许通知，再返回这里测试。',
  default: '尚未允许通知。点击“允许系统通知”，完成浏览器授权。',
  unsupported:
    '当前环境不支持系统通知。请用支持通知的浏览器打开，使用 HTTPS 或本机 localhost 地址。手机和应用内浏览器的支持情况可能不同。',
};

export function systemNotifications({ form, api, flash, refresh }) {
  const area = document.createElement('div');
  area.dataset.systemNotice = '';
  area.hidden = true;
  area.innerHTML = `<p>在当前浏览器所在设备的通知中心显示提醒，无需 API Key。</p>
    <p class="muted">请保持工作室页面打开；关闭页面或手机挂起时暂停接收，重新打开后继续。不支持通知的应用内浏览器可改用系统浏览器。</p>
    <p data-permission role="status"></p><div class="actions"><button type="button" data-enable-notifications>允许系统通知</button><button type="submit" data-test-notification>保存并发送测试通知</button></div>
    <p data-notification-result role="status" class="muted"></p>`;
  form.querySelector('[type=submit]')?.before(area);
  if (!area.isConnected) form.append(area);
  const $ = (s) => area.querySelector(s);
  let busy = false,
    stopped = false,
    registration = null;
  // A receipt failure must never repeat the OS action, even in this tab.
  let pendingReceipt = null;
  function permission() {
    return window.isSecureContext && 'Notification' in window && 'serviceWorker' in navigator
      ? Notification.permission
      : 'unsupported';
  }
  function device(create = false) {
    try {
      let id = localStorage.getItem(DEVICE_KEY);
      if (!/^[a-f0-9]{32}$/.test(id || '')) {
        if (!create) return null;
        id = crypto.randomUUID().replaceAll('-', '');
        localStorage.setItem(DEVICE_KEY, id);
      }
      return id;
    } catch {
      if (create) throw Error('浏览器未允许保存设备标识，请开启本站的本地存储');
      return null;
    }
  }
  function update() {
    const system = form.elements.kind.value === 'system',
      state = permission();
    area.hidden = !system;
    for (const name of ['url', 'credential', 'recipient', 'idempotent']) {
      const input = form.elements[name];
      input.disabled = system;
      input.closest('label').hidden =
        system ||
        (name === 'recipient' && !['telegram', 'slack'].includes(form.elements.kind.value));
    }
    form.elements.url.required = !system;
    if (system) {
      if (!form.elements.id.value) form.elements.id.value = 'system_notice';
      if (!form.elements.title.value) form.elements.title.value = '系统通知';
    }
    $('[data-permission]').textContent = permissionText[state];
    $('[data-enable-notifications]').disabled = ['unsupported', 'granted'].includes(state);
    $('[data-enable-notifications]').textContent =
      state === 'granted' ? '已允许系统通知' : '允许系统通知';
    $('[data-test-notification]').disabled = state === 'unsupported';
  }
  async function enable() {
    // Invoke from the click/submit gesture, before any network operation.
    const state = permission();
    if (state === 'unsupported') throw Error(permissionText.unsupported);
    if (state === 'denied') throw Error(permissionText.denied);
    const result = state === 'granted' ? 'granted' : await Notification.requestPermission();
    update();
    if (result !== 'granted') throw Error(permissionText[result]);
    device(true);
  }
  async function worker() {
    if (registration?.active) return registration;
    registration = await navigator.serviceWorker.register('/assets/notification-worker.js');
    if (!registration.active) {
      const candidate = registration.installing || registration.waiting;
      if (!candidate) throw Error('通知服务未能启动，请刷新页面后重试');
      await new Promise((resolve, reject) => {
        const done = () => {
          if (candidate.state === 'activated') {
            cleanup();
            resolve();
          } else if (candidate.state === 'redundant') {
            cleanup();
            reject(Error('通知服务启动失败'));
          }
        };
        const timer = setTimeout(() => {
          cleanup();
          reject(Error('通知服务启动超时'));
        }, 10000);
        const cleanup = () => {
          clearTimeout(timer);
          candidate.removeEventListener('statechange', done);
        };
        candidate.addEventListener('statechange', done);
        done();
      });
    }
    return registration;
  }
  async function receipt() {
    if (!pendingReceipt) return;
    const { id, body } = pendingReceipt;
    await api(`/v1/connections/system/${encodeURIComponent(id)}/receipt`, 'POST', body);
    pendingReceipt = null;
    window.dispatchEvent(new Event('eah:notification-delivery'));
  }
  async function drain() {
    if (busy || stopped) return;
    busy = true;
    try {
      await receipt();
      if (permission() !== 'granted' || !device()) return;
      // Prepare the browser transport before claiming a durable delivery.
      const service = await worker(),
        device_id = device();
      for (let count = 0; count < 5; count++) {
        const { notification: n } = await api('/v1/connections/system/claim', 'POST', {
          device_id,
        });
        if (!n) return;
        let outcome = 'submitted',
          reason;
        try {
          await service.showNotification(n.title, {
            body: n.text,
            tag: 'eah-' + n.id,
            renotify: false,
            data: { target: '/#runs' },
          });
        } catch {
          outcome = 'failed';
          reason = permission() !== 'granted' ? 'permission_denied' : 'notification_error';
        }
        pendingReceipt = {
          id: n.id,
          body: { device_id, claim_token: n.claim_token, outcome, ...(reason ? { reason } : {}) },
        };
        await receipt();
      }
    } finally {
      busy = false;
    }
  }
  async function test(id) {
    await enable();
    const queued = await api(`/v1/connections/${encodeURIComponent(id)}/test-system`, 'POST', {
      device_id: device(true),
    });
    $('[data-notification-result]').textContent =
      '测试通知已入队，正在请求系统显示。请同时查看系统通知中心和下方投递记录。';
    await drain();
    await refresh();
    const delivery = (await api('/v1/connections/deliveries')).find(
      (d) => d.id === queued.delivery_id
    );
    if (delivery?.status === 'submitted')
      $('[data-notification-result]').textContent =
        '浏览器已接受测试通知，请查看系统通知中心。是否显示横幅由系统设置决定。';
    else if (delivery?.status === 'failed')
      $('[data-notification-result]').textContent =
        delivery.error || '测试通知提交失败，请检查通知权限。';
  }
  async function save(sendTest) {
    if (sendTest) await enable();
    await api('/v1/connections', 'POST', {
      id: form.elements.id.value,
      title: form.elements.title.value,
      kind: 'system',
      device_id: device(true),
    });
    if (sendTest) await test(form.elements.id.value);
    else $('[data-notification-result]').textContent = '系统通知已保存；请允许通知并保持页面打开。';
    flash(sendTest ? '已保存并提交测试，请查看通知中心与投递记录' : '系统通知已保存');
  }
  function bindSaved(root, connections) {
    for (const button of root.querySelectorAll('[data-test-system]')) {
      const c = connections.find((c) => c.id === button.dataset.testSystem),
        current = c.device_id === device();
      button.disabled = !current || permission() === 'unsupported';
      button.textContent = current ? '发送测试通知' : '绑定在其他浏览器';
      button.onclick = async () => {
        button.disabled = true;
        try {
          await test(c.id);
        } catch (e) {
          flash(e.message);
        } finally {
          button.disabled = !current || permission() === 'unsupported';
        }
      };
    }
  }
  $('[data-enable-notifications]').onclick = async () => {
    try {
      await enable();
      await drain();
    } catch (e) {
      flash(e.message);
    }
  };
  window.addEventListener('focus', () => {
    update();
    drain().catch(() => {});
  });
  window.addEventListener('pagehide', () => {
    stopped = true;
  });
  window.addEventListener('pageshow', () => {
    stopped = false;
  });
  setInterval(() => {
    if (!stopped) drain().catch(() => {});
  }, 4000);
  update();
  return { update, save, bindSaved };
}
