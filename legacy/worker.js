const BASE_URL = 'https://wispbyte.com';
const ACCOUNTS_KEY = 'wispbyte_accounts';

// ========== 工具函数 ==========
function log(level, msg) {
  var time = new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });
  var icon = { INFO: '✅', WARN: '⚠️', ERROR: '❌', DEBUG: '🔍' }[level] || 'ℹ️';
  console.log('[' + time + '] ' + icon + ' ' + msg);
}

function jsonResponse(data, status) {
  return new Response(JSON.stringify(data, null, 2), {
    status: status || 200,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type'
    }
  });
}

function htmlResponse(html) {
  return new Response(html, {
    headers: { 'Content-Type': 'text/html; charset=utf-8' }
  });
}

// ========== KV 账号管理 ==========
async function getAccounts(env) {
  try {
    var data = await env.WISPBYTE_KV.get(ACCOUNTS_KEY, 'json');
    return data || [];
  } catch (e) {
    log('ERROR', 'KV读取失败: ' + e.message);
    return [];
  }
}

async function saveAccounts(env, accounts) {
  await env.WISPBYTE_KV.put(ACCOUNTS_KEY, JSON.stringify(accounts));
}

async function addAccount(env, name, cookie) {
  var accounts = await getAccounts(env);
  var existing = accounts.findIndex(function(a) { return a.name === name; });
  var now = new Date().toISOString();

  if (existing >= 0) {
    accounts[existing].cookie = cookie;
    accounts[existing].updatedAt = now;
    accounts[existing].cookieUpdatedAt = now;
  } else {
    accounts.push({ name: name, cookie: cookie, addedAt: now, updatedAt: now, cookieUpdatedAt: now });
  }

  await saveAccounts(env, accounts);
  return accounts.length;
}

async function removeAccountFromKV(env, name) {
  var accounts = await getAccounts(env);
  var newAccounts = accounts.filter(function(a) { return a.name !== name; });
  await saveAccounts(env, newAccounts);
  return accounts.length - newAccounts.length;
}

// ⭐ 核心：被动更新Cookie（从响应头提取set-cookie）
async function updateAccountCookie(env, name, newCookie) {
  if (!newCookie) return;
  var accounts = await getAccounts(env);
  var account = accounts.find(function(a) { return a.name === name; });
  if (account && account.cookie !== newCookie) {
    account.cookie = newCookie;
    account.cookieUpdatedAt = new Date().toISOString();
    await saveAccounts(env, accounts);
    log('INFO', '[' + name + '] Cookie已被动续期');
  }
}

// ========== Telegram通知 ==========
async function sendTelegram(env, text) {
  try {
    var token = env.TELEGRAM_BOT_TOKEN;
    var chatId = env.TELEGRAM_CHAT_ID;
    if (!token || !chatId) return;
    await fetch('https://api.telegram.org/bot' + token + '/sendMessage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, text: text })
    });
  } catch (e) {
    log('DEBUG', 'Telegram通知失败: ' + e.message);
  }
}

// 手动重启单个账号时仍发送通知
async function notifySingle(env, opts) {
  var time = new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });
  var text = '';

  if (opts.serverId) {
    text = (opts.ok ? '✅ 重启成功' : '❌ 重启失败') + '\n\n📧 ' + opts.account + '\n🖥️ ' + opts.serverId + '\n⏰ ' + time;
  } else if (opts.servers && opts.servers.length > 0) {
    var list = opts.servers.map(function(s) { return s.id; }).join(', ');
    text = (opts.ok ? '✅ 重启成功' : '❌ 重启失败') + '\n\n📧 ' + opts.account + '\n🖥️ ' + list + '\n⏰ ' + time;
  }

  if (text) await sendTelegram(env, text + '\n\n🤖 Wispbyte Auto Restart');
}

// ========== API请求（带Cookie被动续期）==========
async function apiRequest(path, cookie, options) {
  var opts = options || {};
  var url = BASE_URL + path;
  var headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.97 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Cookie': cookie
  };

  if (opts.headers) {
    Object.keys(opts.headers).forEach(function(k) { headers[k] = opts.headers[k]; });
  }

  var fetchOpts = { headers: headers, redirect: 'follow' };
  if (opts.method) fetchOpts.method = opts.method;
  if (opts.body) fetchOpts.body = opts.body;

  log('DEBUG', '请求: ' + (opts.method || 'GET') + ' ' + path);
  var response = await fetch(url, fetchOpts);
  log('DEBUG', '响应: ' + response.status);

  var newCookie = null;
  var setCookie = response.headers.get('set-cookie');
  if (setCookie) {
    newCookie = setCookie.split(',').map(function(c) { return c.split(';')[0]; }).join('; ');
    log('DEBUG', '检测到set-cookie，Cookie可续期');
  }

  return { response: response, newCookie: newCookie };
}

// ========== Cookie有效性检查 ==========
async function checkCookieValidity(cookie) {
  try {
    var result = await apiRequest('/client', cookie);
    var html = await result.response.text();

    if (html.includes('login') && !html.includes('logout') && !html.includes('dashboard')) {
      return { valid: false, newCookie: result.newCookie, html: html };
    }

    return { valid: true, newCookie: result.newCookie, html: html };
  } catch (e) {
    return { valid: false, newCookie: null, html: '' };
  }
}

// ========== 获取服务器ID ==========
async function getServerIds(html) {
  var serverIds = [];
  var seen = {};

  // 方法1: 上下文匹配
  var patterns = [
    /data-server[^=]*=["']([a-f0-9]{8})["']/gi,
    /server[_-]?id["'\s:=]+["']?([a-f0-9]{8})["']?/gi,
    /openServer\s*\(\s*["']([a-f0-9]{8})["']\s*\)/gi,
    /loadServer\s*\(\s*["']([a-f0-9]{8})["']\s*\)/gi,
    /\/servers\/([a-f0-9]{8})(?:\/|["'])/gi,
    /server[^>]{0,100}?([a-f0-9]{8})/gi,
    /data-[^=]*=["']([a-f0-9]{8})["']/gi,
    /(?:onclick|href)[^>]{0,50}?([a-f0-9]{8})/gi
  ];

  patterns.forEach(function(pattern) {
    var match;
    while ((match = pattern.exec(html)) !== null) {
      var id = match[1].toLowerCase();
      if (!seen[id]) { seen[id] = true; serverIds.push(id); }
    }
  });

  // 方法2: 备用 - 从页面尾部提取
  if (serverIds.length === 0) {
    var regex = /(?<![a-f0-9-])([a-f0-9]{8})(?![a-f0-9-])/gi;
    var m;
    var allIds = [];
    while ((m = regex.exec(html)) !== null) {
      var id2 = m[1].toLowerCase();
      if (!/^[0-9]{8}$/.test(id2) && id2 !== '00000000' && id2 !== 'ffffffff') {
        allIds.push(id2);
      }
    }
    var last20 = allIds.slice(-20);
    last20.forEach(function(id3) {
      if (!seen[id3]) { seen[id3] = true; serverIds.push(id3); }
    });
  }

  return serverIds;
}

// ========== 验证服务器ID ==========
async function validateServer(cookie, serverId) {
  try {
    var result = await apiRequest('/client/servers/' + serverId + '/console', cookie);
    if (result.response.status === 200) {
      var html = await result.response.text();
      if (html.includes('console') || html.includes('server') || html.includes('restart')) {
        return { valid: true, newCookie: result.newCookie };
      }
    }
    return { valid: false, newCookie: result.newCookie };
  } catch (e) {
    return { valid: false, newCookie: null };
  }
}

// ========== 获取账号的有效服务器 ==========
async function getAccountServers(env, account) {
  var check = await checkCookieValidity(account.cookie);

  if (check.newCookie) {
    await updateAccountCookie(env, account.name, check.newCookie);
  }

  if (!check.valid) {
    throw new Error('Cookie已过期，请手动更新');
  }

  var cookieToUse = check.newCookie || account.cookie;
  var candidateIds = await getServerIds(check.html);
  log('INFO', '[' + account.name + '] 找到 ' + candidateIds.length + ' 个候选ID');

  var validServers = [];
  var toCheck = candidateIds.slice(0, 15);

  for (var i = 0; i < toCheck.length; i++) {
    var vResult = await validateServer(cookieToUse, toCheck[i]);
    if (vResult.valid) {
      validServers.push(toCheck[i]);
      log('INFO', '[' + account.name + '] ✅ 有效服务器: ' + toCheck[i]);
    }
    if (vResult.newCookie) {
      cookieToUse = vResult.newCookie;
      await updateAccountCookie(env, account.name, vResult.newCookie);
    }
    await new Promise(function(r) { setTimeout(r, 300); });
  }

  return { servers: validServers, cookie: cookieToUse };
}

// ========== 重启单个服务器 ==========
async function restartSingleServer(env, accountName, cookie, serverId) {
  var cookieToUse = cookie;

  var consoleResult = await apiRequest('/client/servers/' + serverId + '/console', cookieToUse);
  if (consoleResult.newCookie) {
    cookieToUse = consoleResult.newCookie;
    await updateAccountCookie(env, accountName, consoleResult.newCookie);
  }

  if (consoleResult.response.status !== 200) {
    throw new Error('服务器 ' + serverId + ' 不存在');
  }

  var html = await consoleResult.response.text();
  var csrfToken = '';
  var csrfMatch = html.match(/name="csrf-token"\s+content="([^"]+)"/);
  if (csrfMatch) csrfToken = csrfMatch[1];

  var restartResult = await apiRequest('/client/api/server/restart', cookieToUse, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-TOKEN': csrfToken,
      'X-Requested-With': 'XMLHttpRequest',
      'Referer': BASE_URL + '/client/servers/' + serverId + '/console'
    },
    body: JSON.stringify({ serverId: serverId })
  });

  if (restartResult.newCookie) {
    await updateAccountCookie(env, accountName, restartResult.newCookie);
  }

  if (restartResult.response.status !== 200) {
    throw new Error('重启失败，状态码: ' + restartResult.response.status);
  }

  var responseText = await restartResult.response.text();
  try {
    var data = JSON.parse(responseText);
    if (data.message) log('INFO', '服务器响应: ' + data.message);
  } catch (e) {}

  return true;
}

// ========== 重启账号的所有服务器 ==========
async function restartAccountServers(env, account, specificServerId) {
  var results = { account: account.name, servers: [], success: 0, failed: 0 };

  try {
    var check = await checkCookieValidity(account.cookie);
    if (check.newCookie) {
      await updateAccountCookie(env, account.name, check.newCookie);
    }

    if (!check.valid) {
      results.error = 'Cookie已过期，请手动更新Cookie';
      return results;
    }

    var cookieToUse = check.newCookie || account.cookie;
    var serverIds;

    if (specificServerId) {
      serverIds = [specificServerId];
    } else {
      var candidateIds = await getServerIds(check.html);
      var valid = [];
      for (var i = 0; i < Math.min(candidateIds.length, 15); i++) {
        var vr = await validateServer(cookieToUse, candidateIds[i]);
        if (vr.valid) valid.push(candidateIds[i]);
        if (vr.newCookie) {
          cookieToUse = vr.newCookie;
          await updateAccountCookie(env, account.name, vr.newCookie);
        }
        await new Promise(function(r) { setTimeout(r, 300); });
      }
      serverIds = valid;
    }

    if (serverIds.length === 0) {
      results.error = '未找到服务器';
      return results;
    }

    log('INFO', '[' + account.name + '] 准备重启 ' + serverIds.length + ' 个服务器');

    for (var j = 0; j < serverIds.length; j++) {
      try {
        var accts = await getAccounts(env);
        var freshAcct = accts.find(function(a) { return a.name === account.name; });
        var latestCookie = freshAcct ? freshAcct.cookie : cookieToUse;

        await restartSingleServer(env, account.name, latestCookie, serverIds[j]);
        results.servers.push({ id: serverIds[j], status: 'success' });
        results.success++;
        log('INFO', '[' + account.name + '] ✅ 服务器 ' + serverIds[j] + ' 重启成功');

        // 仅当手动指定单个服务器时发送通知
        if (specificServerId) {
          await notifySingle(env, { ok: true, account: account.name, serverId: serverIds[j] });
        }
      } catch (e) {
        results.servers.push({ id: serverIds[j], status: 'failed: ' + e.message });
        results.failed++;
        log('ERROR', '[' + account.name + '] ❌ 服务器 ' + serverIds[j] + ' 失败: ' + e.message);

        if (specificServerId) {
          await notifySingle(env, { ok: false, account: account.name, serverId: serverIds[j] });
        }
      }
      await new Promise(function(r) { setTimeout(r, 2000); });
    }
  } catch (e) {
    results.error = e.message;
  }

  return results;
}

// ========== 生成并发送汇总重启报告 ==========
async function sendSummaryReport(env, allResults) {
  var report = '📊 重启报告\n\n';

  allResults.forEach(function(r) {
    report += '账号: ' + r.account + '\n';
    if (r.error && r.error.includes('Cookie已过期')) {
      report += '⚠️ Cookie 过期\n';
    } else if (r.servers.length === 0) {
      var errMsg = r.error || '未找到服务器';
      report += '❌ ' + errMsg + '\n';
    } else {
      r.servers.forEach(function(srv) {
        report += '服务器: ' + srv.id + '\n';
        if (srv.status === 'success') {
          report += '✅ 重启成功\n';
        } else {
          report += '❌ 重启失败\n';
        }
      });
    }
    report += '\n'; // 账号之间空行
  });

  report += 'Wispbyte Auto Restart';

  await sendTelegram(env, report);
}

// ========== 重启所有账号 ==========
async function restartAllAccounts(env) {
  log('INFO', '========== 开始批量重启 ==========');
  var accounts = await getAccounts(env);

  if (accounts.length === 0) {
    return { success: false, message: '没有配置任何账号', results: [] };
  }

  var allResults = [];
  var totalSuccess = 0;
  var totalFailed = 0;

  for (var i = 0; i < accounts.length; i++) {
    log('INFO', '📦 处理账号: ' + accounts[i].name);
    var result = await restartAccountServers(env, accounts[i]);
    allResults.push(result);
    totalSuccess += result.success;
    totalFailed += result.failed;

    await new Promise(function(r) { setTimeout(r, 3000); });
  }

  log('INFO', '========== 批量重启完成 ==========');

  await sendSummaryReport(env, allResults);

  return {
    success: true,
    message: '处理了 ' + accounts.length + ' 个账号',
    summary: { totalSuccess: totalSuccess, totalFailed: totalFailed },
    results: allResults
  };
}

// ========== 生成前端HTML ==========
function getHtmlPage() {
  var parts = [];
  parts.push('<!DOCTYPE html>');
  parts.push('<html lang="zh-CN">');
  parts.push('<head>');
  parts.push('<meta charset="UTF-8">');
  parts.push('<meta name="viewport" content="width=device-width, initial-scale=1.0">');
  parts.push('<title>Wispbyte 多账号管理</title>');
  parts.push('<style>');
  parts.push('* { margin: 0; padding: 0; box-sizing: border-box; }');
  parts.push('body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); min-height: 100vh; color: #fff; padding: 20px; }');
  parts.push('.container { max-width: 900px; margin: 0 auto; }');
  parts.push('.header { text-align: center; padding: 30px 0; }');
  parts.push('.header h1 { font-size: 2rem; background: linear-gradient(90deg, #00d2ff, #3a7bd5); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 10px; }');
  parts.push('.header p { color: #888; font-size: 14px; }');
  parts.push('.card { background: rgba(255,255,255,0.05); border-radius: 16px; padding: 24px; margin-bottom: 20px; border: 1px solid rgba(255,255,255,0.1); }');
  parts.push('.card-title { font-size: 1.1rem; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }');
  parts.push('.auth-section { display: flex; gap: 10px; }');
  parts.push('.auth-section input { flex: 1; padding: 12px 16px; border: 1px solid rgba(255,255,255,0.2); border-radius: 8px; background: rgba(0,0,0,0.3); color: #fff; font-size: 14px; }');
  parts.push('.auth-section input:focus { outline: none; border-color: #3a7bd5; }');
  parts.push('.btn { padding: 12px 24px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 500; transition: all 0.3s; display: inline-flex; align-items: center; gap: 6px; }');
  parts.push('.btn:active { transform: scale(0.97); }');
  parts.push('.btn-primary { background: linear-gradient(90deg, #00d2ff, #3a7bd5); color: #fff; }');
  parts.push('.btn-primary:hover { box-shadow: 0 5px 20px rgba(58,123,213,0.4); }');
  parts.push('.btn-danger { background: #e74c3c; color: #fff; }');
  parts.push('.btn-success { background: #27ae60; color: #fff; }');
  parts.push('.btn-warning { background: #f39c12; color: #fff; }');
  parts.push('.btn-sm { padding: 8px 14px; font-size: 12px; }');
  parts.push('textarea { width: 100%; padding: 16px; border: 1px solid rgba(255,255,255,0.2); border-radius: 8px; background: rgba(0,0,0,0.3); color: #fff; font-size: 14px; font-family: Monaco, Consolas, monospace; resize: vertical; min-height: 120px; }');
  parts.push('textarea:focus { outline: none; border-color: #3a7bd5; }');
  parts.push('textarea::placeholder { color: #555; }');
  parts.push('.hint { color: #888; font-size: 12px; margin-top: 8px; line-height: 1.8; }');
  parts.push('.actions { display: flex; gap: 10px; margin-top: 16px; flex-wrap: wrap; }');
  parts.push('.account-item { display: flex; align-items: center; justify-content: space-between; padding: 16px; background: rgba(0,0,0,0.2); border-radius: 8px; margin-bottom: 10px; border-left: 3px solid #3a7bd5; }');
  parts.push('.account-item.expired { border-left-color: #e74c3c; background: rgba(231,76,60,0.05); }');
  parts.push('.account-info { flex: 1; min-width: 0; }');
  parts.push('.account-name { font-weight: 500; margin-bottom: 4px; word-break: break-all; }');
  parts.push('.account-meta { font-size: 12px; color: #888; }');
  parts.push('.account-servers { font-size: 12px; color: #27ae60; margin-top: 4px; }');
  parts.push('.account-actions { display: flex; gap: 6px; flex-wrap: wrap; margin-left: 12px; }');
  parts.push('.tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; }');
  parts.push('.tag-green { background: rgba(39,174,96,0.2); color: #27ae60; }');
  parts.push('.tag-yellow { background: rgba(243,156,18,0.2); color: #f39c12; }');
  parts.push('.tag-red { background: rgba(231,76,60,0.2); color: #e74c3c; }');
  parts.push('.status-bar { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); padding: 12px 24px; border-radius: 8px; font-size: 14px; display: none; z-index: 1000; max-width: 90%; text-align: center; box-shadow: 0 4px 20px rgba(0,0,0,0.3); }');
  parts.push('.status-bar.success { background: #27ae60; display: block; }');
  parts.push('.status-bar.error { background: #e74c3c; display: block; }');
  parts.push('.status-bar.loading { background: #3a7bd5; display: block; }');
  parts.push('.empty-state { text-align: center; padding: 40px; color: #666; }');
  parts.push('.stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 20px; }');
  parts.push('.stat-item { background: rgba(0,0,0,0.2); padding: 20px; border-radius: 12px; text-align: center; }');
  parts.push('.stat-value { font-size: 2rem; font-weight: bold; background: linear-gradient(90deg, #00d2ff, #3a7bd5); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }');
  parts.push('.stat-label { font-size: 12px; color: #888; margin-top: 4px; }');
  parts.push('.modal-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.7); z-index: 2000; justify-content: center; align-items: center; }');
  parts.push('.modal-overlay.active { display: flex; }');
  parts.push('.modal { background: #1a1a2e; border: 1px solid rgba(255,255,255,0.1); border-radius: 16px; padding: 24px; width: 90%; max-width: 500px; }');
  parts.push('.modal h3 { margin-bottom: 16px; }');
  parts.push('.modal textarea { min-height: 80px; margin-bottom: 12px; }');
  parts.push('.modal-actions { display: flex; gap: 10px; justify-content: flex-end; }');
  parts.push('@media (max-width: 600px) { .stats { grid-template-columns: 1fr; } .auth-section { flex-direction: column; } .account-item { flex-direction: column; align-items: flex-start; gap: 12px; } .account-actions { width: 100%; margin-left: 0; } .account-actions .btn { flex: 1; } }');
  parts.push('</style>');
  parts.push('</head>');
  parts.push('<body>');
  parts.push('<div class="container">');
  parts.push('<div class="header">');
  parts.push('<h1>🚀 Wispbyte 多账号管理</h1>');
  parts.push('<p>自动重启 · Cookie被动续期 · 过期提醒</p>');
  parts.push('</div>');

  parts.push('<div class="card">');
  parts.push('<div class="card-title">🔐 API 密钥</div>');
  parts.push('<div class="auth-section">');
  parts.push('<input type="password" id="authKey" placeholder="输入 AUTH_KEY 密钥..." onkeydown="if(event.key===\'Enter\')loadAccounts()">');
  parts.push('<button class="btn btn-primary" onclick="loadAccounts()">连接</button>');
  parts.push('</div>');
  parts.push('</div>');

  parts.push('<div class="stats" id="stats" style="display: none;">');
  parts.push('<div class="stat-item"><div class="stat-value" id="statAccounts">0</div><div class="stat-label">账号数量</div></div>');
  parts.push('<div class="stat-item"><div class="stat-value" id="statServers">0</div><div class="stat-label">服务器数量</div></div>');
  parts.push('<div class="stat-item"><div class="stat-value" id="statStatus">-</div><div class="stat-label">状态</div></div>');
  parts.push('</div>');

  parts.push('<div class="card" id="addSection" style="display: none;">');
  parts.push('<div class="card-title">➕ 添加账号</div>');
  parts.push('<textarea id="accountInput" placeholder="格式：邮箱-----Cookie\n\n示例：\nuser@gmail.com-----connect.sid=s%3Axxx...\n\n每行一个，可批量添加"></textarea>');
  parts.push('<div class="hint">💡 每行一个账号，格式：邮箱-----Cookie（5个减号分隔）<br>🔄 Cookie会在每次请求时自动被动续期<br>🔴 有CF验证无法自动登录，Cookie过期需手动更新</div>');
  parts.push('<div class="actions">');
  parts.push('<button class="btn btn-primary" onclick="addAccounts()">📥 添加账号</button>');
  parts.push('<button class="btn btn-success" onclick="restartAll()">🔄 重启全部</button>');
  parts.push('</div>');
  parts.push('</div>');

  parts.push('<div class="card" id="listSection" style="display: none;">');
  parts.push('<div class="card-title">📋 账号列表</div>');
  parts.push('<div id="accountList"><div class="empty-state">暂无账号</div></div>');
  parts.push('</div>');
  parts.push('</div>');

  parts.push('<div class="modal-overlay" id="cookieModal">');
  parts.push('<div class="modal">');
  parts.push('<h3>🍪 更新Cookie</h3>');
  parts.push('<p style="color: #888; font-size: 13px; margin-bottom: 12px;" id="modalAccountLabel">账号：</p>');
  parts.push('<input type="hidden" id="modalAccountName">');
  parts.push('<textarea id="modalCookieInput" placeholder="粘贴新的Cookie..."></textarea>');
  parts.push('<div class="modal-actions">');
  parts.push('<button class="btn btn-sm" onclick="closeCookieModal()" style="background: #555;">取消</button>');
  parts.push('<button class="btn btn-primary btn-sm" onclick="submitCookieUpdate()">更新</button>');
  parts.push('</div>');
  parts.push('</div>');
  parts.push('</div>');

  parts.push('<div class="status-bar" id="statusBar"></div>');

  parts.push('<script>');
  parts.push('var API_BASE = window.location.origin;');
  parts.push('var authKey = "";');
  parts.push('');
  parts.push('function showStatus(msg, type) {');
  parts.push('  var bar = document.getElementById("statusBar");');
  parts.push('  bar.textContent = msg;');
  parts.push('  bar.className = "status-bar " + (type || "loading");');
  parts.push('  if (type && type !== "loading") setTimeout(function() { bar.className = "status-bar"; }, 4000);');
  parts.push('}');
  parts.push('');
  parts.push('function api(path, options) {');
  parts.push('  var sep = path.indexOf("?") >= 0 ? "&" : "?";');
  parts.push('  var url = API_BASE + path + sep + "key=" + encodeURIComponent(authKey);');
  parts.push('  return fetch(url, options || {}).then(function(r) { return r.json(); });');
  parts.push('}');
  parts.push('');

  parts.push('function loadAccounts() {');
  parts.push('  authKey = document.getElementById("authKey").value;');
  parts.push('  if (!authKey) { showStatus("请输入密钥", "error"); return; }');
  parts.push('  localStorage.setItem("wispbyte_auth_key", authKey);');
  parts.push('  showStatus("加载中...");');
  parts.push('  api("/accounts").then(function(data) {');
  parts.push('    if (!data.success && data.message === "Unauthorized") { showStatus("密钥错误", "error"); return; }');
  parts.push('    document.getElementById("stats").style.display = "grid";');
  parts.push('    document.getElementById("addSection").style.display = "block";');
  parts.push('    document.getElementById("listSection").style.display = "block";');
  parts.push('    var accts = data.accounts || [];');
  parts.push('    document.getElementById("statAccounts").textContent = accts.length;');
  parts.push('    renderAccounts(accts);');
  parts.push('    showStatus("加载成功", "success");');
  parts.push('    loadServers();');
  parts.push('  }).catch(function(e) { showStatus("连接失败: " + e.message, "error"); });');
  parts.push('}');
  parts.push('');

  parts.push('function loadServers() {');
  parts.push('  api("/servers").then(function(data) {');
  parts.push('    if (!data.success || !data.results) return;');
  parts.push('    var totalServers = 0;');
  parts.push('    data.results.forEach(function(r) {');
  parts.push('      totalServers += r.count || 0;');
  parts.push('      var el = document.querySelector("[data-account=\\"" + r.account + "\\"] .account-servers");');
  parts.push('      var item = document.querySelector("[data-account=\\"" + r.account + "\\"]");');
  parts.push('      if (r.error) {');
  parts.push('        if (el) el.innerHTML = "<span style=\\"color:#e74c3c\\">❌ " + r.error + "</span>";');
  parts.push('        if (item) item.classList.add("expired");');
  parts.push('      } else {');
  parts.push('        if (el) el.textContent = "🖥️ 服务器: " + (r.servers.join(", ") || "无");');
  parts.push('      }');
  parts.push('    });');
  parts.push('    document.getElementById("statServers").textContent = totalServers;');
  parts.push('    document.getElementById("statStatus").textContent = "正常";');
  parts.push('  }).catch(function(e) { console.error(e); });');
  parts.push('}');
  parts.push('');

  parts.push('function renderAccounts(accounts) {');
  parts.push('  var list = document.getElementById("accountList");');
  parts.push('  if (accounts.length === 0) { list.innerHTML = "<div class=\\"empty-state\\">暂无账号，请添加</div>"; return; }');
  parts.push('  var html = "";');
  parts.push('  accounts.forEach(function(a) {');
  parts.push('    var cookieAge = a.cookieUpdatedAt ? Math.floor((Date.now() - new Date(a.cookieUpdatedAt).getTime()) / 1000 / 60) : null;');
  parts.push('    var tagHtml = "";');
  parts.push('    if (cookieAge !== null) {');
  parts.push('      if (cookieAge < 60) tagHtml = "<span class=\\"tag tag-green\\">🟢 刚更新</span>";');
  parts.push('      else if (cookieAge < 1440) tagHtml = "<span class=\\"tag tag-yellow\\">🟡 " + Math.floor(cookieAge/60) + "h前</span>";');
  parts.push('      else tagHtml = "<span class=\\"tag tag-red\\">🔴 " + Math.floor(cookieAge/1440) + "天前</span>";');
  parts.push('    }');
  parts.push('    html += "<div class=\\"account-item\\" data-account=\\"" + a.name + "\\">";');
  parts.push('    html += "<div class=\\"account-info\\">";');
  parts.push('    html += "<div class=\\"account-name\\">📧 " + a.name + " " + tagHtml + "</div>";');
  parts.push('    html += "<div class=\\"account-meta\\">Cookie: " + a.cookieLength + " 字符 · " + new Date(a.addedAt).toLocaleString("zh-CN") + "</div>";');
  parts.push('    html += "<div class=\\"account-servers\\">🖥️ 服务器: 加载中...</div>";');
  parts.push('    html += "</div>";');
  parts.push('    html += "<div class=\\"account-actions\\">";');
  parts.push('    html += "<button class=\\"btn btn-warning btn-sm\\" onclick=\\"openCookieModal(\'" + a.name + "\')\\">🍪 更新</button>";');
  parts.push('    html += "<button class=\\"btn btn-success btn-sm\\" onclick=\\"restartAccount(\'" + a.name + "\')\\">🔄 重启</button>";');
  parts.push('    html += "<button class=\\"btn btn-danger btn-sm\\" onclick=\\"removeAccount(\'" + a.name + "\')\\">🗑️</button>";');
  parts.push('    html += "</div></div>";');
  parts.push('  });');
  parts.push('  list.innerHTML = html;');
  parts.push('}');
  parts.push('');

  parts.push('function addAccounts() {');
  parts.push('  var input = document.getElementById("accountInput").value.trim();');
  parts.push('  if (!input) { showStatus("请输入账号信息", "error"); return; }');
  parts.push('  var lines = input.split("\\n").filter(function(l) { return l.trim(); });');
  parts.push('  var accounts = [];');
  parts.push('  lines.forEach(function(line) {');
  parts.push('    var idx = line.indexOf("-----");');
  parts.push('    if (idx > 0) {');
  parts.push('      accounts.push({ name: line.substring(0, idx).trim(), cookie: line.substring(idx + 5).trim() });');
  parts.push('    }');
  parts.push('  });');
  parts.push('  if (accounts.length === 0) { showStatus("格式错误，用 ----- 分隔", "error"); return; }');
  parts.push('  showStatus("添加中...");');
  parts.push('  api("/accounts/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ accounts: accounts }) })');
  parts.push('  .then(function(data) {');
  parts.push('    if (data.success) { showStatus("添加成功: " + data.imported + " 个", "success"); document.getElementById("accountInput").value = ""; loadAccounts(); }');
  parts.push('    else showStatus("失败: " + data.message, "error");');
  parts.push('  }).catch(function(e) { showStatus(e.message, "error"); });');
  parts.push('}');
  parts.push('');

  parts.push('function removeAccount(name) {');
  parts.push('  if (!confirm("确定删除 " + name + " ？")) return;');
  parts.push('  api("/accounts/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name }) })');
  parts.push('  .then(function(data) { if (data.success) { showStatus("已删除", "success"); loadAccounts(); } else showStatus(data.message, "error"); })');
  parts.push('  .catch(function(e) { showStatus(e.message, "error"); });');
  parts.push('}');
  parts.push('');

  parts.push('function restartAccount(name) {');
  parts.push('  showStatus("重启中...");');
  parts.push('  api("/restart?account=" + encodeURIComponent(name))');
  parts.push('  .then(function(data) {');
  parts.push('    if (data.success > 0) showStatus("重启成功: " + data.success + " 个服务器", "success");');
  parts.push('    else showStatus(data.error || data.message || "未找到服务器", "error");');
  parts.push('  }).catch(function(e) { showStatus(e.message, "error"); });');
  parts.push('}');
  parts.push('');

  parts.push('function restartAll() {');
  parts.push('  if (!confirm("确定重启所有账号的服务器？")) return;');
  parts.push('  showStatus("批量重启中...");');
  parts.push('  api("/restart-all").then(function(data) {');
  parts.push('    if (data.success) {');
  parts.push('      var msg = "完成: " + data.summary.totalSuccess + " 成功, " + data.summary.totalFailed + " 失败";');
  parts.push('      if (data.summary.expired > 0) msg += ", " + data.summary.expired + " 个Cookie过期";');
  parts.push('      showStatus(msg, data.summary.totalFailed === 0 ? "success" : "error");');
  parts.push('    } else showStatus(data.message, "error");');
  parts.push('  }).catch(function(e) { showStatus(e.message, "error"); });');
  parts.push('}');
  parts.push('');

  parts.push('function openCookieModal(name) {');
  parts.push('  document.getElementById("modalAccountName").value = name;');
  parts.push('  document.getElementById("modalAccountLabel").textContent = "账号：" + name;');
  parts.push('  document.getElementById("modalCookieInput").value = "";');
  parts.push('  document.getElementById("cookieModal").classList.add("active");');
  parts.push('}');
  parts.push('');
  parts.push('function closeCookieModal() {');
  parts.push('  document.getElementById("cookieModal").classList.remove("active");');
  parts.push('}');
  parts.push('');
  parts.push('function submitCookieUpdate() {');
  parts.push('  var name = document.getElementById("modalAccountName").value;');
  parts.push('  var cookie = document.getElementById("modalCookieInput").value.trim();');
  parts.push('  if (!cookie) { showStatus("请输入Cookie", "error"); return; }');
  parts.push('  showStatus("更新中...");');
  parts.push('  api("/accounts/update-cookie", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name, cookie: cookie }) })');
  parts.push('  .then(function(data) {');
  parts.push('    if (data.success) { showStatus("Cookie已更新", "success"); closeCookieModal(); loadAccounts(); }');
  parts.push('    else showStatus(data.message, "error");');
  parts.push('  }).catch(function(e) { showStatus(e.message, "error"); });');
  parts.push('}');
  parts.push('');

  parts.push('var savedKey = localStorage.getItem("wispbyte_auth_key");');
  parts.push('if (savedKey) { document.getElementById("authKey").value = savedKey; }');
  parts.push('</script>');
  parts.push('</body>');
  parts.push('</html>');

  return parts.join('\n');
}

// ========== Worker 入口 ==========
export default {
  async fetch(request, env, ctx) {
    var url = new URL(request.url);

    if (url.pathname === '/' && !url.searchParams.has('key')) {
      return htmlResponse(getHtmlPage());
    }

    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type'
        }
      });
    }

    var authKey = url.searchParams.get('key');
    if (env.AUTH_KEY && authKey !== env.AUTH_KEY) {
      return jsonResponse({ success: false, message: 'Unauthorized' }, 401);
    }

    try {
      if (url.pathname === '/accounts' && request.method === 'GET') {
        var accounts = await getAccounts(env);
        return jsonResponse({
          success: true,
          accounts: accounts.map(function(a) {
            return {
              name: a.name,
              cookieLength: (a.cookie || '').length,
              addedAt: a.addedAt,
              cookieUpdatedAt: a.cookieUpdatedAt
            };
          })
        });
      }

      if (url.pathname === '/accounts/remove' && request.method === 'POST') {
        var body1 = await request.json();
        if (!body1.name) return jsonResponse({ success: false, message: '缺少 name' }, 400);
        var removed = await removeAccountFromKV(env, body1.name);
        return jsonResponse({ success: removed > 0, message: removed > 0 ? '已删除' : '不存在' });
      }

      if (url.pathname === '/accounts/update-cookie' && request.method === 'POST') {
        var body2 = await request.json();
        if (!body2.name || !body2.cookie) return jsonResponse({ success: false, message: '缺少参数' }, 400);

        var accounts2 = await getAccounts(env);
        var acct = accounts2.find(function(a) { return a.name === body2.name; });
        if (!acct) return jsonResponse({ success: false, message: '账号不存在' }, 404);

        acct.cookie = body2.cookie;
        acct.cookieUpdatedAt = new Date().toISOString();
        acct.updatedAt = new Date().toISOString();
        await saveAccounts(env, accounts2);

        return jsonResponse({ success: true, message: 'Cookie已更新' });
      }

      if (url.pathname === '/accounts/import' && request.method === 'POST') {
        var body3 = await request.json();
        if (!Array.isArray(body3.accounts)) return jsonResponse({ success: false, message: '需要 accounts 数组' }, 400);

        var imported = 0, importFailed = 0;
        for (var i = 0; i < body3.accounts.length; i++) {
          var item = body3.accounts[i];
          if (!item.name || !item.cookie) { importFailed++; continue; }
          try {
            await addAccount(env, item.name, item.cookie);
            imported++;
          } catch (e) { importFailed++; }
        }

        return jsonResponse({ success: true, imported: imported, failed: importFailed });
      }

      if (url.pathname === '/servers') {
        var accountName = url.searchParams.get('account');
        var sAccounts = await getAccounts(env);

        if (accountName) {
          var sa = sAccounts.find(function(a) { return a.name === accountName; });
          if (!sa) return jsonResponse({ success: false, message: '账号不存在' }, 404);
          try {
            var sr = await getAccountServers(env, sa);
            return jsonResponse({ success: true, account: accountName, servers: sr.servers, count: sr.servers.length });
          } catch (e) {
            return jsonResponse({ success: true, account: accountName, error: e.message, servers: [], count: 0 });
          }
        }

        var results = [];
        for (var i2 = 0; i2 < sAccounts.length; i2++) {
          try {
            var r = await getAccountServers(env, sAccounts[i2]);
            results.push({ account: sAccounts[i2].name, servers: r.servers, count: r.servers.length });
          } catch (e) {
            results.push({ account: sAccounts[i2].name, error: e.message, servers: [], count: 0 });
          }
          await new Promise(function(resolve) { setTimeout(resolve, 500); });
        }

        return jsonResponse({ success: true, results: results });
      }

      if (url.pathname === '/restart') {
        var rName = url.searchParams.get('account');
        var rServer = url.searchParams.get('server');
        var rAccounts = await getAccounts(env);

        if (!rName) {
          var allResult = await restartAllAccounts(env);
          return jsonResponse(allResult);
        }

        var ra = rAccounts.find(function(a) { return a.name === rName; });
        if (!ra) return jsonResponse({ success: false, message: '账号不存在' }, 404);

        var rResult = await restartAccountServers(env, ra, rServer || null);
        return jsonResponse({
          success: rResult.failed === 0 && rResult.success > 0,
          account: rResult.account,
          servers: rResult.servers,
          success: rResult.success,
          failed: rResult.failed,
          error: rResult.error
        });
      }

      if (url.pathname === '/restart-all') {
        var allRestartResult = await restartAllAccounts(env);
        return jsonResponse(allRestartResult);
      }

      if (url.pathname === '/status') {
        var stAccounts = await getAccounts(env);
        return jsonResponse({
          success: true,
          accountCount: stAccounts.length,
          accounts: stAccounts.map(function(a) {
            return { name: a.name, cookieUpdatedAt: a.cookieUpdatedAt, cookieLength: (a.cookie || '').length };
          })
        });
      }

      return htmlResponse(getHtmlPage());

    } catch (e) {
      log('ERROR', '请求处理失败: ' + e.message);
      return jsonResponse({ success: false, message: '服务器错误: ' + e.message }, 500);
    }
  },

  async scheduled(event, env, ctx) {
    log('INFO', '⏰ 定时任务触发');
    await restartAllAccounts(env);
  }
};
