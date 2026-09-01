/**
 * 记忆管理 — 弹窗式管理界面
 * 在"更多工具"下拉菜单中点击"记忆管理"打开
 */
(function () {
    "use strict";

    const API_BASE = "/api/memory";

    // ── 弹窗控制 ──
    function show() {
        document.getElementById("memoryOverlay").style.display = "block";
        document.getElementById("memoryModal").style.display = "flex";
        switchTab("mem");  // 默认显示记忆检索 tab
        loadStats();
        loadEntries();
    }

    function hide() {
        document.getElementById("memoryOverlay").style.display = "none";
        document.getElementById("memoryModal").style.display = "none";
    }

    // ── Tab 切换 ──
    function switchTab(name) {
        document.querySelectorAll(".memory-tab").forEach(t => t.classList.remove("active"));
        document.querySelectorAll(".memory-tab-panel").forEach(p => p.classList.remove("active"));
        document.querySelector(`.memory-tab[data-tab="${name}"]`)?.classList.add("active");
        document.getElementById(`tab-${name}`)?.classList.add("active");

        if (name === "profile") loadProfile();
        if (name === "debug") loadEmbedStats();  // 懒加载
        if (name === "config") loadConfigTab();  // 懒加载
    }

    // ── API ──
    async function loadStats() {
        try {
            const resp = await fetch(`${API_BASE}/stats`);
            const data = await resp.json();
            let html = `后端: <strong>${data.backend}</strong> | ` +
                `FTS5: <strong>${data.semantic_count}</strong> | ` +
                `情景: <strong>${data.episodic_count}</strong> | ` +
                `FTS5 DB: <strong>${data.db_size_mb} MB</strong>`;
            if (data.embedding_available) {
                html += ` | 向量: <strong>${data.embedding_count || 0}</strong>`;
                if (data.embedding_size_mb != null) {
                    html += ` (${data.embedding_size_mb} MB)`;
                }
            }
            document.getElementById("memStatsBar").innerHTML = html;
        } catch (e) {
            document.getElementById("memStatsBar").textContent = "加载失败";
        }
    }

    async function loadEntries() {
        try {
            const resp = await fetch(`${API_BASE}/entries?limit=30`);
            const data = await resp.json();
            if (!data.items || data.items.length === 0) {
                document.getElementById("memResults").innerHTML =
                    '<p class="mem-hint">暂无记忆条目，进行一次股票分析后将自动记录</p>';
                return;
            }
            renderEntries(data.items, false);
        } catch (e) {
            document.getElementById("memResults").innerHTML =
                '<p class="mem-hint">加载失败</p>';
        }
    }

    async function doSearch() {
        const q = document.getElementById("memSearchInput").value.trim();
        if (!q) { await loadEntries(); return; }
        const type = document.getElementById("memSearchType").value;
        try {
            const resp = await fetch(
                `${API_BASE}/search?q=${encodeURIComponent(q)}&type=${type}&top_k=20`
            );
            const data = await resp.json();
            if (!data.items || data.items.length === 0) {
                document.getElementById("memResults").innerHTML =
                    '<p class="mem-hint">未找到匹配的记忆</p>';
                return;
            }
            renderEntries(data.items, true);
        } catch (e) {
            document.getElementById("memResults").innerHTML =
                '<p class="mem-hint">搜索失败</p>';
        }
    }

    async function doClean(dryRun) {
        const before = document.getElementById("memCleanBefore").value;
        if (!before) return;
        const el = document.getElementById("memCleanResult");
        el.textContent = dryRun ? "预览中..." : "清理中...";
        try {
            const resp = await fetch(`${API_BASE}/clean`, {
                method: "DELETE",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ before, dry_run: dryRun }),
            });
            const data = await resp.json();
            el.textContent = dryRun
                ? `预览: 将删除 ${data.deleted_count} 条`
                : `已删除 ${data.deleted_count} 条`;
            loadStats();
            loadEntries();
        } catch (e) {
            el.textContent = "操作失败";
        }
    }

    async function doCleanStock(dryRun) {
        const code = document.getElementById("memCleanStock").value.trim();
        if (!code) return;
        const el = document.getElementById("memCleanStockResult");
        el.textContent = dryRun ? "预览中..." : "清理中...";
        try {
            const resp = await fetch(`${API_BASE}/clean`, {
                method: "DELETE",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ stock_code: code, dry_run: dryRun }),
            });
            const data = await resp.json();
            el.textContent = dryRun
                ? `预览: 将删除 ${data.deleted_count} 条`
                : `已删除 ${data.deleted_count} 条`;
            loadStats();
            loadEntries();
        } catch (e) {
            el.textContent = "操作失败";
        }
    }

    // ── 渲染 ──
    function renderEntries(items, showScore) {
        let html = '<div class="mem-entry-list">';
        for (const item of items) {
            const typeLabel = item.type === "episodic" ? "情景" : "语义";
            const typeCls = item.type === "episodic" ? "mem-type-episodic" : "mem-type-semantic";
            const date = (item.date || "").slice(0, 16);
            const tags = (item.tags || [])
                .slice(0, 5)
                .map(t => `<span class="mem-tag">${escHtml(t)}</span>`)
                .join("");
            const scoreInfo = showScore && item.score
                ? `<span class="mem-score">相关度: ${item.score.toFixed(2)}</span>`
                : "";
            // v2: 置信度标签
            const conf = item.confidence || 0;
            let confBadge = "";
            if (conf > 0) {
                const confLabel = conf >= 0.8 ? "🟢高信" : conf >= 0.5 ? "🟡中信" : "🔴低信";
                confBadge = `<span class="mem-confidence" title="置信度: ${conf.toFixed(2)}">${confLabel}</span>`;
            }
            const title = item.stock_name
                ? `${escHtml(item.stock_name)} (${escHtml(item.stock_code)})`
                : (item.content || "").slice(0, 80);
            const eid = item.entry_id || "";
            // 溯源按钮（仅语义记忆）
            const traceBtn = (item.type !== "episodic" && eid)
                ? `<button class="mem-trace-btn" onclick="window.StockRadar.MemoryManager.showProvenance('${eid}')" title="溯源">🔍</button>`
                : "";
            html += `
            <div class="mem-entry" id="mem-${eid}">
                <div class="mem-entry-header">
                    <span class="mem-type-badge ${typeCls}">${typeLabel}</span>
                    <span class="mem-title">${title}</span>
                    ${scoreInfo}
                    ${confBadge}
                    <span class="mem-date">${date}</span>
                    ${traceBtn}
                    <button class="mem-del-btn" onclick="window.StockRadar.MemoryManager.deleteEntry('${eid}')" title="删除">×</button>
                </div>
                <div class="mem-entry-body">${escHtml((item.content || "").slice(0, 200))}</div>
                ${tags ? `<div class="mem-entry-tags">${tags}</div>` : ""}
            </div>`;
        }
        html += "</div>";
        document.getElementById("memResults").innerHTML = html;
    }

    function escHtml(s) {
        const div = document.createElement("div");
        div.textContent = s || "";
        return div.innerHTML;
    }

    // ── 事件绑定（DOM 加载后） ──
    function bind() {
        document.getElementById("memoryManagerBtn").onclick = show;
        document.getElementById("memoryCloseBtn").onclick = hide;
        document.getElementById("memoryOverlay").onclick = hide;
        document.getElementById("memSearchBtn").onclick = doSearch;
        document.getElementById("memSearchInput").onkeydown = (e) => {
            if (e.key === "Enter") doSearch();
        };
        document.getElementById("memRefreshBtn").onclick = async () => {
            await loadStats();
            await loadEntries();
        };
        document.getElementById("memCleanPreviewBtn").onclick = () => doClean(true);
        document.getElementById("memCleanExecBtn").onclick = () => doClean(false);
        document.getElementById("memCleanStockPreviewBtn").onclick = () => doCleanStock(true);
        document.getElementById("memCleanStockExecBtn").onclick = () => doCleanStock(false);
        document.getElementById("memResetBtn").onclick = doResetAll;

        // ── Tab 切换 ──
        document.querySelectorAll(".memory-tab").forEach(btn => {
            btn.onclick = () => switchTab(btn.dataset.tab);
        });

        // ── 调试工具 ──
        document.getElementById("memDebugTokenizeBtn").onclick = doDebugTokenize;
        document.getElementById("memDebugEmbedBtn").onclick = doDebugEmbed;
        document.getElementById("memDebugSimBtn").onclick = doDebugSimilarity;
        document.getElementById("memDebugRetrieveBtn").onclick = doDebugRetrieve;
        document.getElementById("memConfigSaveBtn").onclick = saveConfigTab;
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bind);
    } else {
        bind();
    }

    async function doResetAll() {
        if (!confirm("确认清空全部记忆数据？\n这将删除：语义记忆 + 情景记忆 + 向量数据 + 用户画像\n此操作不可恢复！")) return;
        const el = document.getElementById("memResetResult");
        el.textContent = "清空中...";
        try {
            const resp = await fetch(`${API_BASE}/reset`, { method: "DELETE" });
            const data = await resp.json();
            el.textContent =
                `已清空: 语义 ${data.fts5_deleted} | 向量 ${data.embedding_deleted} | 情景 ${data.episodic_deleted} | 画像 ${data.profile_reset ? '已重置' : '跳过'}`;
            loadStats();
            loadEntries();
        } catch (e) {
            el.textContent = "清空失败";
        }
    }

    async function deleteEntry(entryId) {
        if (!confirm("确定删除这条记忆？")) return;
        try {
            await fetch(`${API_BASE}/entries/${entryId}`, { method: "DELETE" });
            document.getElementById(`mem-${entryId}`)?.remove();
            loadStats();
        } catch (e) {
            alert("删除失败");
        }
    }

    // ── v2: 溯源 ──

    async function showProvenance(entryId) {
        try {
            const resp = await fetch(`${API_BASE}/entries/${entryId}/provenance`);
            const data = await resp.json();
            if (data.error) {
                alert("溯源失败: " + data.error);
                return;
            }
            // 构建弹窗内容
            let html = `<div style="font-size:12px;line-height:1.6;max-height:400px;overflow-y:auto;">`;
            html += `<p><b>标的:</b> ${escHtml(data.stock_name)} (${escHtml(data.stock_code)})</p>`;
            html += `<p><b>日期:</b> ${escHtml(data.date || "")}</p>`;
            html += `<p><b>置信度:</b> ${(data.confidence || 0).toFixed(2)}</p>`;
            if (data.memory_category) {
                html += `<p><b>记忆类型:</b> ${escHtml(data.memory_category)} | 过期: ${escHtml(data.expires_at || "永久")}</p>`;
            }
            const prov = data.provenance || {};
            if (prov.tool_calls && prov.tool_calls.length > 0) {
                html += `<p><b>工具调用:</b></p><ul>`;
                prov.tool_calls.forEach(tc => {
                    html += `<li>${escHtml(tc.name)} — 入参: ${escHtml((tc.input_keys || []).join(", "))}</li>`;
                });
                html += `</ul>`;
            }
            if (prov.reasoning_summary) {
                html += `<p><b>推理摘要:</b> ${escHtml(prov.reasoning_summary)}</p>`;
            }
            if (prov.trace_run_id) {
                html += `<p><b>Trace:</b> <code>${escHtml(prov.trace_run_id)}</code></p>`;
            }
            if (data.full_chain) {
                const fc = data.full_chain;
                html += `<hr><p><b>完整决策链 (trace):</b></p>`;
                if (fc.tool_calls) {
                    html += `<p>工具步骤: ${fc.tool_calls.length} 个</p>`;
                }
                if (fc.reasoning_path) {
                    html += `<p style="color:var(--muted)">${escHtml(fc.reasoning_path.slice(0, 500))}</p>`;
                }
            }
            html += `</div>`;
            document.getElementById("memTraceDetail").innerHTML = html;
            document.getElementById("memTraceModal").style.display = "block";
        } catch (e) {
            alert("溯源请求失败: " + e.message);
        }
    }

    // ── 调试工具 ──

    async function doDebugTokenize() {
        const text = document.getElementById("memDebugTokenizeInput").value.trim();
        const el = document.getElementById("memDebugTokenizeResult");
        if (!text) { el.textContent = "请输入文本"; return; }
        el.textContent = "分词中...";
        try {
            const resp = await fetch(`${API_BASE}/debug/tokenize`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text }),
            });
            const data = await resp.json();
            if (data.error) { el.textContent = "错误: " + data.error; return; }
            el.textContent = `共 ${data.count} 个词: ` + data.tokens.join(" | ");
        } catch (e) {
            el.textContent = "请求失败";
        }
    }

    async function doDebugEmbed() {
        const text = document.getElementById("memDebugEmbedInput").value.trim();
        const el = document.getElementById("memDebugEmbedResult");
        if (!text) { el.textContent = "请输入文本"; return; }
        el.textContent = "Embedding...";
        try {
            const resp = await fetch(`${API_BASE}/debug/embed`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text }),
            });
            const data = await resp.json();
            if (data.error) { el.textContent = "错误: " + data.error; return; }
            el.textContent = `维度: ${data.dim} | 模长: ${data.norm} | 前10: [${data.first_10.join(", ")}]`;
        } catch (e) {
            el.textContent = "请求失败";
        }
    }

    async function doDebugSimilarity() {
        const text1 = document.getElementById("memDebugSim1").value.trim();
        const text2 = document.getElementById("memDebugSim2").value.trim();
        const el = document.getElementById("memDebugSimResult");
        if (!text1 || !text2) { el.textContent = "请输入两段文本"; return; }
        el.textContent = "计算中...";
        try {
            const resp = await fetch(`${API_BASE}/debug/similarity`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text1, text2 }),
            });
            const data = await resp.json();
            if (data.error) { el.textContent = "错误: " + data.error; return; }
            const pct = (data.similarity * 100).toFixed(1);
            el.textContent = `余弦相似度: ${data.similarity.toFixed(4)} (${pct}%) | 维度: ${data.dim}`;
        } catch (e) {
            el.textContent = "请求失败";
        }
    }

    // ── Embedding 统计仪表盘 ──

    async function loadEmbedStats() {
        const el = document.getElementById("memDebugEmbedStats");
        if (!el) return;
        el.innerHTML = '<span class="mem-hint">加载中...</span>';
        try {
            const resp = await fetch(`${API_BASE}/debug/embed-stats`);
            const data = await resp.json();
            if (!data.available) { el.innerHTML = '<span class="mem-hint">Embedding 未启用</span>'; return; }
            if (data.error) { el.innerHTML = `<span class="mem-hint">错误: ${data.error}</span>`; return; }

            let html = `<div class="mem-debug-stats">
                <strong>向量总数:</strong> ${data.total_vectors}`;
            if (data.top_stocks && Object.keys(data.top_stocks).length > 0) {
                html += ' | ';
                html += Object.entries(data.top_stocks).slice(0, 5).map(([k, v]) => `${escHtml(k)}(×${v})`).join(' ');
            }
            html += '</div>';

            // 条目明细表
            if (data.items && data.items.length > 0) {
                html += '<table class="mem-embed-table"><thead><tr><th>股票</th><th>日期</th><th>内容</th><th>标签</th></tr></thead><tbody>';
                for (const item of data.items) {
                    html += `<tr>
                        <td style="white-space:nowrap">${escHtml(item.stock_name || item.stock_code || '-')}</td>
                        <td style="white-space:nowrap">${item.date}</td>
                        <td>${escHtml(item.content)}</td>
                        <td>${(item.tags || []).map(t => `<span class="mem-tag">${escHtml(t)}</span>`).join('')}</td>
                    </tr>`;
                }
                html += '</tbody></table>';
            }
            el.innerHTML = html;
        } catch (e) {
            el.innerHTML = '<span class="mem-hint">加载失败</span>';
        }
    }

    // ── 检索模拟 ──

    async function doDebugRetrieve() {
        const q = document.getElementById("memDebugRetrieveInput").value.trim();
        const el = document.getElementById("memDebugRetrieveResult");
        if (!q) { el.innerHTML = '<span class="mem-hint">请输入检索问题</span>'; return; }
        el.innerHTML = '<span class="mem-hint">检索中...</span>';
        try {
            const resp = await fetch(`${API_BASE}/debug/retrieve`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ query: q, top_k: 10 }),
            });
            const data = await resp.json();
            if (data.error) { el.innerHTML = `<span class="mem-hint">${escHtml(data.error)}</span>`; return; }

            let html = `<div style="margin-bottom:4px">在 ${data.total_in_db} 条向量中找到 ${data.found} 条匹配 (${data.elapsed_ms}ms) | Agent 上下文最多取 ${data.simulate_top_k} 条</div>`;
            if (data.items.length === 0) {
                html += '<span class="mem-hint">无匹配结果</span>';
            } else {
                for (const item of data.items) {
                    html += `<div class="mem-debug-result-item">
                        <span class="mem-score">${item.similarity.toFixed(4)}</span>
                        <span class="mem-date">${escHtml(item.date)}</span>
                        <strong>${escHtml(item.stock)}</strong>
                        <div>${escHtml(item.content)}</div>
                    </div>`;
                }
            }
            el.innerHTML = html;
        } catch (e) {
            el.innerHTML = '<span class="mem-hint">检索失败</span>';
        }
    }

    // ── 记忆系统配置（min_similarity 等） ──

    async function loadConfigTab() {
        const el = document.getElementById("memConfigResult");
        try {
            const resp = await fetch(`${API_BASE}/config`);
            const data = await resp.json();
            document.getElementById("memMinSimInput").value =
                (data.min_similarity != null) ? data.min_similarity : 0.72;
            if (el) el.textContent = `后端: ${data.backend || "?"}`;
        } catch (e) {
            if (el) el.textContent = "加载失败";
        }
    }

    async function saveConfigTab() {
        const el = document.getElementById("memConfigResult");
        const raw = document.getElementById("memMinSimInput").value.trim();
        const v = parseFloat(raw);
        if (isNaN(v) || v < 0 || v > 1) {
            if (el) el.textContent = "请输入 0~1 之间的数值";
            return;
        }
        try {
            const resp = await fetch(`${API_BASE}/config`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ min_similarity: v }),
            });
            const data = await resp.json();
            if (data.success) {
                if (el) el.textContent = `已保存 min_similarity = ${data.min_similarity}`;
            } else {
                if (el) el.textContent = "保存失败";
            }
        } catch (e) {
            if (el) el.textContent = "保存失败: " + e.message;
        }
    }


    // ── 用户画像 ──

    async function loadProfile() {
        try {
            const resp = await fetch(`${API_BASE}/profile`);
            const data = await resp.json();
            let html = '<div class="mem-profile-grid">';
            html += `<div class="mem-profile-item"><label>风险偏好</label><span>${escHtml(data.risk_tolerance || '未设置')}</span></div>`;
            html += `<div class="mem-profile-item"><label>投资周期</label><span>${escHtml(data.investment_horizon || '未设置')}</span></div>`;
            html += `<div class="mem-profile-item"><label>分析风格</label><span>${escHtml(data.analysis_style || '未设置')}</span></div>`;
            html += '</div>';

            if (data.watched_stocks && Object.keys(data.watched_stocks).length > 0) {
                html += '<div class="mem-profile-tags"><label>关注个股</label>';
                for (const [k, v] of Object.entries(data.watched_stocks).slice(0, 5)) {
                    html += `<span class="mem-tag">${escHtml(k)} (${v}次)</span>`;
                }
                html += '</div>';
            }
            if (data.watched_sectors && Object.keys(data.watched_sectors).length > 0) {
                html += '<div class="mem-profile-tags"><label>关注板块</label>';
                for (const [k, v] of Object.entries(data.watched_sectors).slice(0, 5)) {
                    html += `<span class="mem-tag">${escHtml(k)} (${v}次)</span>`;
                }
                html += '</div>';
            }
            if (data.recent_queries && data.recent_queries.length > 0) {
                html += '<div class="mem-profile-queries"><label>最近查询</label>';
                for (const q of data.recent_queries.slice(0, 5)) {
                    html += `<div class="mem-profile-query">${escHtml(q.query)}</div>`;
                }
                html += '</div>';
            }

            // 编辑区
            html += '<div class="mem-profile-edit">';
            html += '<select id="memProfileRisk" class="mem-profile-select">';
            for (const [v, label] of [['', '风险偏好...'], ['conservative', '稳健型'], ['moderate', '平衡型'], ['aggressive', '激进型']]) {
                html += `<option value="${v}" ${data.risk_tolerance === v ? 'selected' : ''}>${label}</option>`;
            }
            html += '</select>';
            html += '<select id="memProfileHorizon" class="mem-profile-select">';
            for (const [v, label] of [['', '投资周期...'], ['short', '短线'], ['medium', '中线'], ['long', '长线']]) {
                html += `<option value="${v}" ${data.investment_horizon === v ? 'selected' : ''}>${label}</option>`;
            }
            html += '</select>';
            html += '<select id="memProfileStyle" class="mem-profile-select">';
            for (const [v, label] of [['', '分析风格...'], ['technical_heavy', '技术面为主'], ['fundamental_heavy', '基本面为主'], ['balanced', '均衡']]) {
                html += `<option value="${v}" ${data.analysis_style === v ? 'selected' : ''}>${label}</option>`;
            }
            html += '</select>';
            html += '<button onclick="window.StockRadar.MemoryManager.saveProfile()" class="mem-btn-ghost">保存</button>';
            html += '</div>';

            document.getElementById("memProfileContent").innerHTML = html;
        } catch (e) {
            document.getElementById("memProfileContent").innerHTML = '<span class="mem-hint">加载失败</span>';
        }
    }

    async function saveProfile() {
        const body = {
            risk_tolerance: document.getElementById("memProfileRisk")?.value || '',
            investment_horizon: document.getElementById("memProfileHorizon")?.value || '',
            analysis_style: document.getElementById("memProfileStyle")?.value || '',
        };
        try {
            await fetch(`${API_BASE}/profile`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            loadProfile();
        } catch (e) {
            alert('保存失败');
        }
    }

    // ── 暴露 ──
    window.StockRadar = window.StockRadar || {};
    window.StockRadar.MemoryManager = { show, hide, deleteEntry, saveProfile, showProvenance };
})();
