// Preview only passive media from authenticated artifact storage, never arbitrary HTML/SVG.
const types = new Map([
  ['image/png', 'img'],
  ['image/jpeg', 'img'],
  ['image/webp', 'img'],
  ['image/gif', 'img'],
  ['video/mp4', 'video'],
  ['video/webm', 'video'],
  ['audio/mpeg', 'audio'],
  ['audio/wav', 'audio'],
  ['audio/ogg', 'audio'],
]);
const active = new WeakMap();
export const canPreview = (mime) => types.has(mime);
export function clearMedia(root) {
  for (const url of active.get(root) || []) URL.revokeObjectURL(url);
  active.delete(root);
}
export function bindMedia(root, token, guard) {
  const urls = [];
  active.set(root, urls);
  root.querySelectorAll('[data-media-preview]').forEach(
    (button) =>
      (button.onclick = guard(async () => {
        const kind = types.get(button.dataset.mediaType);
        if (!kind) return;
        button.disabled = true;
        try {
          const response = await fetch(
            '/v1/artifacts/' + encodeURIComponent(button.dataset.mediaPreview) + '/content',
            { headers: token ? { Authorization: 'Bearer ' + token } : {} }
          );
          if (!response.ok) throw Error('无法加载媒体产物');
          const blob = await response.blob();
          if (!types.has(blob.type)) throw Error('此文件类型不支持预览');
          if (active.get(root) !== urls) return;
          const url = URL.createObjectURL(blob);
          urls.push(url);
          const media = document.createElement(kind);
          media.src = url;
          media.className = 'artifact-preview';
          if (kind === 'img') media.alt = button.dataset.filename || '生成的图片';
          else {
            media.controls = true;
            media.preload = 'metadata';
          }
          button.insertAdjacentElement('afterend', media);
          button.hidden = true;
        } finally {
          button.disabled = false;
        }
      }))
  );
}

export async function uploadMedia(file, token, limit = 2_000_000) {
  if (!file.size || file.size > limit)
    throw Error('请选择不超过 ' + Math.floor(limit / 1_000_000) + ' MB 的文件');
  const response = await fetch('/v1/artifacts/upload?name=' + encodeURIComponent(file.name), {
    method: 'POST',
    body: file,
    headers: {
      'Content-Type': file.type || 'application/octet-stream',
      ...(token ? { Authorization: 'Bearer ' + token } : {}),
    },
  });
  if (!response.ok) throw Error('参考文件上传失败（' + response.status + '）');
  return response.json();
}
