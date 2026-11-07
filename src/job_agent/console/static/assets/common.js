/* job-agent 控制台共享脚本（S5b 页面组①共用；无构建步骤）
 *
 * api()：统一 fetch 封装——非 GET 请求自动携带 S5a 安全头
 *   X-Job-Agent: console（§10.1 第 3 项：防跨站简单请求）；
 *   JSON 请求/响应自动序列化；错误抛 Error(message)，
 *   FastAPI 的 422 detail 数组被拼成可读文案。
 */
/* global fetch */

async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {} };
  if (init.method !== 'GET') init.headers['X-Job-Agent'] = 'console';
  if (opts.formData) {
    init.body = opts.formData;
  } else if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) throw new Error(errText(data, res.status));
  return data;
}

function errText(data, status) {
  // S5a 异常统一 JSON 信封：{"error": {"code", "message"}}（422 含 details）
  if (data && data.error && data.error.message) return String(data.error.message);
  const d = data && data.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d) && d.length) {
    return d.map(function (x) {
      const loc = Array.isArray(x.loc) ? x.loc.slice(1).join('.') : '';
      return (loc ? loc + ': ' : '') + (x.msg || '');
    }).join('；');
  }
  return '请求失败（HTTP ' + status + '）';
}

const $ = function (id) { return document.getElementById(id); };

function esc(s) {
  return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

function showMsg(id, text, isError) {
  const el = $(id);
  el.textContent = text || '';
  el.className = 'msg' + (isError ? ' error' : text ? ' ok' : '');
}

/* 关键词/列表输入：逗号、中文顿号、分号、空白分隔 → 数组 */
function splitList(text) {
  return String(text || '').split(/[,，、;；\s]+/).filter(Boolean);
}

function joinList(arr) { return (arr || []).join('、'); }

function num(v, fallback) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}
