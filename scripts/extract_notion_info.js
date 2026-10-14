/**
 * Notion 账号信息提取脚本（旧版手动备用流程）
 *
 * 推荐优先运行 `python login.py`。该登录脚本会自动打开临时 Chrome/Edge
 * 调试窗口，并从本地浏览器会话中提取 token_v2 和账号/工作区字段。
 *
 * 如果自动登录流程不可用，可以使用本手动脚本：
 * 1. 浏览器登录 https://www.notion.so/ai
 * 2. 确保左上角切换到你要提取的账号
 * 3. F12 → Application → Cookies → 复制 token_v2 的值
 * 4. F12 → Console → 粘贴本脚本 → 回车
 * 5. 如果有多个账号/工作区，按提示选择
 * 6. 把输出的 JSON 粘贴到 accounts.json，替换 YOUR_TOKEN_V2
 */
(async () => {
  try {
    // ─── 第 1 步：获取所有可访问的用户和空间 ───
    // getSpaces 会返回当前 token 能看到的所有用户（含多账号）
    let allUsers = {};  // user_id → {name, email}
    let allSpaces = {}; // space_id → {name, plan, members}
    let spaceViewMap = {}; // space_id → space_view_id

    // 尝试 getSpaces（返回更完整的多账号数据）
    try {
      const r1 = await fetch('/api/v3/getSpaces', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: '{}', credentials: 'include'
      });
      const d1 = await r1.json();
      // getSpaces 返回 { "user_id_1": { space: {...}, ... }, "user_id_2": {...} }
      if (d1 && typeof d1 === 'object' && !d1.recordMap) {
        for (const [userId, userData] of Object.entries(d1)) {
          if (!userData || typeof userData !== 'object') continue;
          // 提取用户信息
          const nu = userData.notion_user;
          if (nu) {
            for (const [nuid, nuData] of Object.entries(nu)) {
              const v = nuData?.value?.value || nuData?.value || nuData || {};
              allUsers[nuid] = {
                name: v.given_name || v.name || v.family_name || '',
                email: v.email || ''
              };
            }
          }
          // 提取空间信息
          const sp = userData.space;
          if (sp) {
            for (const [sid, sData] of Object.entries(sp)) {
              const v = sData?.value?.value || sData?.value || sData || {};
              if (!allSpaces[sid]) {
                allSpaces[sid] = {
                  name: v.name || '',
                  plan: v.plan_type || v.subscription_tier || ''
                };
              }
            }
          }
          // 提取 space_view
          const sv = userData.space_view;
          if (sv) {
            for (const [svid, svData] of Object.entries(sv)) {
              const v = svData?.value?.value || svData?.value || svData || {};
              if (v.space_id) spaceViewMap[v.space_id] = svid;
            }
          }
        }
      }
    } catch (e) { /* getSpaces 失败，fallback 到 loadUserContent */ }

    // fallback：loadUserContent
    const r2 = await fetch('/api/v3/loadUserContent', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: '{}', credentials: 'include'
    });
    const d2 = (await r2.json()).recordMap || {};

    // 合并用户
    for (const [nuid, nuData] of Object.entries(d2.notion_user || {})) {
      if (allUsers[nuid]) continue;
      const v = nuData?.value?.value || nuData?.value || nuData || {};
      allUsers[nuid] = {
        name: v.given_name || v.name || v.family_name || '',
        email: v.email || ''
      };
    }
    // 合并空间
    for (const [sid, sData] of Object.entries(d2.space || {})) {
      if (allSpaces[sid]) continue;
      const v = sData?.value?.value || sData?.value || sData || {};
      allSpaces[sid] = {
        name: v.name || '',
        plan: v.plan_type || v.subscription_tier || ''
      };
    }
    // 合并 space_view
    for (const [svid, svData] of Object.entries(d2.space_view || {})) {
      const v = svData?.value?.value || svData?.value || svData || {};
      if (v.space_id && !spaceViewMap[v.space_id]) spaceViewMap[v.space_id] = svid;
    }

    // 读取 cookie 中的 notion_user_id（当前 UI 上的活跃账号）
    const cookieUserId = document.cookie.split(';')
      .map(c => c.trim())
      .find(c => c.startsWith('notion_user_id='))
      ?.split('=')[1] || '';

    const userList = Object.entries(allUsers);
    const spaceList = Object.entries(allSpaces).map(([id, s]) => ({
      space_id: id, name: s.name, plan: s.plan, space_view_id: spaceViewMap[id] || ''
    }));

    if (userList.length === 0) {
      console.error('❌ 未找到任何用户信息，请确认已登录 Notion');
      return;
    }

    // ─── 展示 & 选择用户 ───
    console.log('\n');
    console.log('%c═══════════════════════════════════════════════', 'color:#00a699');
    console.log('%c  Notion 账号信息提取工具', 'font-size:15px;font-weight:bold;color:#00a699');
    console.log('%c═══════════════════════════════════════════════', 'color:#00a699');
    console.log('');

    let chosenUserId, chosenUserName, chosenUserEmail;

    if (userList.length === 1) {
      chosenUserId = userList[0][0];
      chosenUserName = userList[0][1].name;
      chosenUserEmail = userList[0][1].email;
      console.log(`%c👤 用户: ${chosenUserName || '(未知)'} ${chosenUserEmail ? '(' + chosenUserEmail + ')' : ''}`, 'font-size:13px');
    } else {
      console.log(`%c👥 检测到 ${userList.length} 个 Notion 账号：`, 'font-size:13px;font-weight:bold');
      console.log('');
      userList.forEach(([uid, u], i) => {
        const active = uid === cookieUserId ? ' ← 当前活跃' : '';
        console.log(`%c  [${i}]  ${u.name || '(未知)'} ${u.email ? '(' + u.email + ')' : ''}${active}`, 'font-size:13px');
      });
      console.log('');
      console.log('%c👆 请查看上方账号列表，3 秒后弹窗选择...', 'color:#ff9800;font-size:12px');

      await new Promise(resolve => setTimeout(resolve, 3000));

      const promptText = userList.map(([uid, u], i) => {
        const active = uid === cookieUserId ? ' ← 当前' : '';
        return `[${i}] ${u.name || '(未知)'} ${u.email ? '(' + u.email + ')' : ''}${active}`;
      }).join('\n');

      const idx = prompt(`请选择要提取的账号：\n\n${promptText}\n\n输入编号 (0 ~ ${userList.length - 1})：`);
      if (idx === null || idx.trim() === '') {
        console.log('%c⚠️ 已取消', 'color:#ff9800');
        return;
      }
      const chosen = userList[parseInt(idx)];
      if (!chosen) {
        console.error(`❌ 编号 "${idx}" 无效`);
        return;
      }
      chosenUserId = chosen[0];
      chosenUserName = chosen[1].name;
      chosenUserEmail = chosen[1].email;
    }

    console.log(`%c✅ 选择用户: ${chosenUserName || chosenUserId.slice(0,8)} ${chosenUserEmail ? '(' + chosenUserEmail + ')' : ''}`, 'color:#00c853;font-size:13px');

    // ─── 选择工作区 ───
    if (spaceList.length === 0) {
      console.error('❌ 未找到任何工作区');
      return;
    }

    console.log('');
    console.log(`%c📂 找到 ${spaceList.length} 个工作区：`, 'font-size:13px;font-weight:bold');
    console.log('');
    spaceList.forEach((s, i) => {
      const label = s.name || `(ID: ${s.space_id.slice(0, 13)}...)`;
      const planStr = s.plan ? `  计划: ${s.plan}` : '';
      console.log(`%c  [${i}]  ${label}${planStr}`, 'font-size:13px');
    });

    let chosenSpace;
    if (spaceList.length === 1) {
      chosenSpace = spaceList[0];
      console.log('%c🎯 只有一个工作区，自动选择', 'color:#2196f3;font-weight:bold');
    } else {
      console.log('');
      console.log('%c👆 3 秒后弹窗选择工作区...', 'color:#ff9800;font-size:12px');
      await new Promise(resolve => setTimeout(resolve, 3000));

      const promptText = spaceList.map((s, i) => {
        const label = s.name || `ID: ${s.space_id.slice(0, 13)}...`;
        return `[${i}] ${label}`;
      }).join('\n');

      const idx = prompt(`请选择有 AI 功能的工作区：\n\n${promptText}\n\n输入编号 (0 ~ ${spaceList.length - 1})：`);
      if (idx === null || idx.trim() === '') {
        console.log('%c⚠️ 已取消', 'color:#ff9800');
        return;
      }
      chosenSpace = spaceList[parseInt(idx)];
      if (!chosenSpace) {
        console.error(`❌ 编号 "${idx}" 无效`);
        return;
      }
    }

    // ─── 输出结果 ───
    const account = {
      token_v2: 'YOUR_TOKEN_V2',
      space_id: chosenSpace.space_id,
      user_id: chosenUserId,
      space_view_id: chosenSpace.space_view_id,
      user_name: chosenUserName,
      user_email: chosenUserEmail
    };

    const json = JSON.stringify(account, null, 2);
    const spaceLabel = chosenSpace.name || chosenSpace.space_id.slice(0, 13) + '...';

    console.log('');
    console.log('%c═══════════════════════════════════════════════', 'color:#00c853');
    console.log(`%c✅ 用户: ${chosenUserName || '(未知)'}  工作区: ${spaceLabel}`, 'color:#00c853;font-weight:bold;font-size:14px');
    console.log('%c═══════════════════════════════════════════════', 'color:#00c853');
    console.log('');
    console.log(json);
    console.log('');
    console.log('%c⚠️  下一步：把 YOUR_TOKEN_V2 替换为你复制的 token_v2 值', 'color:#ff9800;font-weight:bold');
    console.log('%c   然后粘贴到 accounts.json 数组中', 'color:#ff9800');
    console.log('%c   ⚠️  注意：token_v2 要用你选择的那个账号的！在 Cookies 里确认', 'color:#ff9800');

    setTimeout(() => {
      navigator.clipboard.writeText(json)
        .then(() => console.log('%c📋 已自动复制到剪贴板', 'color:#00c853'))
        .catch(() => console.log('%c📋 请手动选中上方 JSON 复制', 'color:#ff9800'));
    }, 800);

  } catch (e) { console.error('❌ 提取失败:', e.message) }
})();
