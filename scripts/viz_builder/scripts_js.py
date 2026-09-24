# -*- coding: utf-8 -*-
"""
JavaScript interactive logic for SuperMew Visualization
"""

JS_CONTENT = r"""
// 移动端侧边栏切换与遮罩控制
function toggleMobileSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (sidebar) {
        sidebar.classList.toggle('open');
        if (backdrop) backdrop.classList.toggle('active', sidebar.classList.contains('open'));
    }
}

function closeMobileSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (sidebar) sidebar.classList.remove('open');
    if (backdrop) backdrop.classList.remove('active');
}

function navigatePage(pageId) {
    showPage(pageId);
    if (window.innerWidth <= 960) {
        closeMobileSidebar();
    }
}

// 页面切换与导航高亮
function showPage(pageId) {
    if (window.innerWidth <= 960) {
        closeMobileSidebar();
    }
    const sections = document.querySelectorAll('.details-section');
    sections.forEach(sec => sec.classList.remove('active'));

    const targetSection = document.getElementById(pageId);
    if (targetSection) {
        targetSection.classList.add('active');
        setTimeout(() => {
            renderVisibleMermaid();
        }, 50);
    }

    const navItems = document.querySelectorAll('.file');
    navItems.forEach(item => item.classList.remove('active'));

    const navKey = pageId.replace('page-', 'nav-');
    const targetNav = document.getElementById(navKey);
    if (targetNav) {
        targetNav.classList.add('active');
    }

    window.scrollTo({ top: 0, behavior: 'smooth' });
}

// 动态渲染可见区域的 Mermaid 图
async function renderVisibleMermaid() {
    if (!window.mermaid) return;
    const activeSection = document.querySelector('.details-section.active');
    if (!activeSection) return;
    const mermaidEls = activeSection.querySelectorAll('.mermaid');
    for (const el of mermaidEls) {
        if (!el.getAttribute('data-processed')) {
            const rawCode = el.getAttribute('data-code') || el.textContent.trim();
            if (!el.getAttribute('data-code')) {
                el.setAttribute('data-code', rawCode);
            }
            try {
                const id = 'mermaid-svg-' + Math.random().toString(36).substring(2, 9);
                const { svg } = await mermaid.render(id, rawCode);
                el.innerHTML = svg;
                el.setAttribute('data-processed', 'true');
            } catch (err) {
                console.error('Mermaid render error:', err);
            }
        }
    }
}

// 代码一键复制
function copyCode(btn) {
    const codeBlock = btn.closest('.code-block');
    if (!codeBlock) return;
    const codeEl = codeBlock.querySelector('pre code') || codeBlock.querySelector('pre');
    if (!codeEl) return;
    const text = codeEl.innerText;
    navigator.clipboard.writeText(text).then(() => {
        const originalText = btn.innerText;
        btn.innerText = '已复制!';
        btn.style.borderColor = 'var(--accent-teal)';
        btn.style.color = 'var(--accent-teal)';
        setTimeout(() => {
            btn.innerText = originalText;
            btn.style.borderColor = '';
            btn.style.color = '';
        }, 1800);
    }).catch(err => {
        console.error('复制失败:', err);
    });
}

// Live Trace 步进播放器数据与控制器

const TRACE_STEPS = [
    {
        "step": 1,
        "title": "Step 1: 复杂输入与意图识别",
        "shortTitle": "1. 意图分类",
        "nodeName": "entry_classify",
        "nodeBadge": "Node: entry_classify",
        "nodeColor": "#38bdf8",
        "toolBadge": {
            "text": "FastModel Routing",
            "bg": "rgba(56, 189, 248, 0.15)",
            "color": "#38bdf8"
        },
        "stateBadge": {
            "text": "Initial State",
            "bg": "rgba(148, 163, 184, 0.15)",
            "color": "#94a3b8"
        },
        "cot": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\">\n        <strong style=\"color: #0f172a;\">用户原始查询：</strong>\n        <span style=\"color: #0284c7; font-weight: 600;\">“对比销售部与技术部差旅标准中，关于高铁与住宿的审批权限与金额上限差异”</span>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #38bdf8;\">\n        <div style=\"color: #38bdf8; font-size: 12.5px; font-weight: 700; margin-bottom: 8px;\">🤖 FastModel (Qwen2.5-7B) 意图与复杂度预检：</div>\n        <ul style=\"margin: 0; padding-left: 18px; font-size: 12.5px; color: #cbd5e1; line-height: 1.7;\">\n            <li><strong style=\"color: #f8fafc;\">实体抽取：</strong>销售部差旅政策、技术部差旅政策</li>\n            <li><strong style=\"color: #f8fafc;\">维度定位：</strong>交通方式（高铁）、住宿报销、审批人层级、限额差异</li>\n            <li><strong style=\"color: #f8fafc;\">复杂度评估：</strong><span class=\"pill pill-purple\">COMPLEX_COMPARISON</span></li>\n            <li><strong style=\"color: #f8fafc;\">路由决策：</strong>单次直接检索极易因注意力稀释而丢失部门交叉对比约束，分流至 <code>planner</code> 编排节点。</li>\n        </ul>\n    </div>\n</div>",
        "toolInfo": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\" style=\"margin-bottom: 10px;\">\n        <strong style=\"color: #0f172a;\">调度动作：</strong><code style=\"color: #7c3aed; font-weight: 600;\">GraphRouter.route(query) -&gt; planner</code>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #10b981;\">\n        <div style=\"color: #10b981; font-size: 12px; font-weight: 700; margin-bottom: 6px;\">⚡ 输入验证与分流指标：</div>\n        <div style=\"display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 11.5px; color: #cbd5e1;\">\n            <div>• 命中关键词：<span style=\"color:#f8fafc;\">对比, 审批权限, 金额上限</span></div>\n            <div>• 实体覆盖率：<span style=\"color:#10b981;\">100% (2/2部门)</span></div>\n            <div>• 注入防御：<span style=\"color:#10b981;\">PASS (无越狱Prompt)</span></div>\n            <div>• 预期子任务数：<span style=\"color:#38bdf8;\">2 (并行扇出)</span></div>\n        </div>\n    </div>\n</div>",
        "state": {
            "query": "对比销售部与技术部差旅标准中，关于高铁与住宿的审批权限与金额上限差异",
            "complexity": "COMPLEX_COMPARISON",
            "target_departments": [
                "销售部",
                "技术部"
            ],
            "dimensions": [
                "高铁席别与上限",
                "住宿标额",
                "审批流权限"
            ],
            "sub_queries": [],
            "retrieved_chunks": [],
            "reranked_chunks": [],
            "evidence_grade": null,
            "synthesized_answer": null
        },
        "telemetry": [
            {
                "icon": "⚡",
                "title": "FastModel 意图路由完成",
                "status": "200 OK",
                "desc": "耗时 82ms | Prompt: 48 tokens | 决策: planner 分支"
            },
            {
                "icon": "🛡️",
                "title": "安全审计与注入防护过滤",
                "status": "PASS",
                "desc": "输入清洗完毕，无恶意越狱或系统指令越权"
            }
        ]
    },
    {
        "step": 2,
        "title": "Step 2: 结构化拆解与规划 (Planner)",
        "shortTitle": "2. 问题拆解",
        "nodeName": "planner",
        "nodeBadge": "Node: planner",
        "nodeColor": "#a855f7",
        "toolBadge": {
            "text": "Plan & Fan-out",
            "bg": "rgba(168, 85, 247, 0.15)",
            "color": "#a855f7"
        },
        "stateBadge": {
            "text": "Sub-tasks Created",
            "bg": "rgba(168, 85, 247, 0.15)",
            "color": "#a855f7"
        },
        "cot": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\">\n        <strong style=\"color: #0f172a;\">规划目标：</strong>\n        <span style=\"color: #0284c7; font-weight: 600;\">将宏观对比分解为高内聚、低耦合的独立检索单元</span>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #a855f7;\">\n        <div style=\"color: #a855f7; font-size: 12.5px; font-weight: 700; margin-bottom: 8px;\">📋 Planner 正交分解结果：</div>\n        <div style=\"font-size: 12.5px; color: #cbd5e1; line-height: 1.7;\">\n            <div style=\"margin-bottom: 6px;\">\n                <span style=\"background: rgba(168, 85, 247, 0.25); color: #d8b4fe; padding: 2px 6px; border-radius: 4px; font-weight: 600;\">Sub-Task 1</span>\n                <strong style=\"color: #f8fafc; margin-left: 6px;\">销售部差旅标准：</strong>\n                <span>检索销售部员工在高铁席别（二等座/一等座/商务座）、住宿报销上限及部门总监/财务审批权限。</span>\n            </div>\n            <div>\n                <span style=\"background: rgba(56, 189, 248, 0.25); color: #7dd3fc; padding: 2px 6px; border-radius: 4px; font-weight: 600;\">Sub-Task 2</span>\n                <strong style=\"color: #f8fafc; margin-left: 6px;\">技术部差旅标准：</strong>\n                <span>检索研发/技术中心人员高铁席别要求、外派与出差住宿报销标准及技术VP/HR审批权限。</span>\n            </div>\n        </div>\n    </div>\n</div>",
        "toolInfo": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\" style=\"margin-bottom: 10px;\">\n        <strong style=\"color: #0f172a;\">LangGraph 算子：</strong><code style=\"color: #7c3aed; font-weight: 600;\">[Send(\"rag_sub_agent\", task1), Send(\"rag_sub_agent\", task2)]</code>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #38bdf8;\">\n        <div style=\"color: #38bdf8; font-size: 12px; font-weight: 700; margin-bottom: 6px;\">⚙️ 并行扇出执行通道：</div>\n        <div style=\"font-size: 11.5px; color: #cbd5e1; line-height: 1.6;\">\n            • 并发模式：<code>asyncio.gather</code> 结合 LangGraph Send 状态隔离分发<br>\n            • 共享上下文：<code>state.sub_queries</code> 注入只读引用<br>\n            • 聚合通道：<code>operator.add</code> 自动规约子 Agent 召回的文档列表\n        </div>\n    </div>\n</div>",
        "state": {
            "query": "对比销售部与技术部差旅标准中，关于高铁与住宿的审批权限与金额上限差异",
            "complexity": "COMPLEX_COMPARISON",
            "target_departments": [
                "销售部",
                "技术部"
            ],
            "dimensions": [
                "高铁席别与上限",
                "住宿标额",
                "审批流权限"
            ],
            "sub_queries": [
                {
                    "id": "sub_1",
                    "dept": "销售部",
                    "query": "销售部 差旅标准 高铁席别 住宿报销上限 审批权限"
                },
                {
                    "id": "sub_2",
                    "dept": "技术部",
                    "query": "技术部 研发中心 差旅报销 高铁 住宿限额 审批流程"
                }
            ],
            "retrieved_chunks": [],
            "reranked_chunks": [],
            "evidence_grade": null,
            "synthesized_answer": null
        },
        "telemetry": [
            {
                "icon": "🔀",
                "title": "LangGraph Send 扇出成功",
                "status": "2 Sub-Tasks",
                "desc": "耗时 115ms | 生成 2 个针对性精准检索子问题"
            },
            {
                "icon": "📦",
                "title": "通道准备完毕",
                "status": "READY",
                "desc": "构建隔离的状态载荷，防止跨子任务数据污染"
            }
        ]
    },
    {
        "step": 3,
        "title": "Step 3: 并行扇出与双路混合检索",
        "shortTitle": "3. 混合检索",
        "nodeName": "rag_sub_agent (parallel)",
        "nodeBadge": "Node: rag_sub_agent",
        "nodeColor": "#10b981",
        "toolBadge": {
            "text": "Dense + Sparse Hybrid",
            "bg": "rgba(16, 185, 129, 0.15)",
            "color": "#10b981"
        },
        "stateBadge": {
            "text": "12 Chunks Recalled",
            "bg": "rgba(16, 185, 129, 0.15)",
            "color": "#10b981"
        },
        "cot": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\">\n        <strong style=\"color: #0f172a;\">检索执行策略：</strong>\n        <span style=\"color: #0284c7; font-weight: 600;\">双子任务并发执行 BGE-M3 密集向量 + Milvus 2.5 原生 BM25 稀疏检索</span>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #10b981;\">\n        <div style=\"color: #10b981; font-size: 12.5px; font-weight: 700; margin-bottom: 8px;\">🔍 召回流详情（RRF 倒数排名融合 k=60）：</div>\n        <div style=\"font-size: 12px; color: #cbd5e1; line-height: 1.7;\">\n            <div>• <strong style=\"color:#f8fafc;\">Sub-Task 1 (销售部)：</strong>Dense 召回 6 个 L3 块，BM25 召回 6 个 L3 块，RRF 融合去重后输出 6 个候选块。</div>\n            <div>• <strong style=\"color:#f8fafc;\">Sub-Task 2 (技术部)：</strong>Dense 召回 6 个 L3 块，BM25 召回 6 个 L3 块，RRF 融合去重后输出 6 个候选块。</div>\n            <div style=\"color: #38bdf8; margin-top: 4px;\">🎯 两路共召回 12 个叶子切片 (Level 3)，平均粒度 680 字符，保留完整 Markdown 锚点信息。</div>\n        </div>\n    </div>\n</div>",
        "toolInfo": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\" style=\"margin-bottom: 10px;\">\n        <strong style=\"color: #0f172a;\">存储调用：</strong><code style=\"color: #7c3aed; font-weight: 600;\">milvus.hybrid_search(dense_vec, bm25_tokens, limit=12)</code>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #38bdf8;\">\n        <div style=\"color: #38bdf8; font-size: 12px; font-weight: 700; margin-bottom: 6px;\">📊 召回分值分布（前 4 名展示）：</div>\n        <table style=\"width: 100%; font-size: 11px; color: #cbd5e1; border-collapse: collapse;\">\n            <tr style=\"border-bottom: 1px solid rgba(255,255,255,0.1); color: #94a3b8;\">\n                <th style=\"text-align: left; padding: 3px 0;\">Chunk ID</th><th>Dense Rank</th><th>BM25 Rank</th><th>RRF Score</th>\n            </tr>\n            <tr><td style=\"color: #f8fafc; font-family: monospace;\">sales_doc_c03</td><td align=\"center\">1</td><td align=\"center\">2</td><td align=\"center\" style=\"color:#10b981;\">0.0325</td></tr>\n            <tr><td style=\"color: #f8fafc; font-family: monospace;\">sales_doc_c04</td><td align=\"center\">2</td><td align=\"center\">1</td><td align=\"center\" style=\"color:#10b981;\">0.0325</td></tr>\n            <tr><td style=\"color: #f8fafc; font-family: monospace;\">tech_doc_c08</td><td align=\"center\">1</td><td align=\"center\">3</td><td align=\"center\" style=\"color:#10b981;\">0.0320</td></tr>\n            <tr><td style=\"color: #f8fafc; font-family: monospace;\">tech_doc_c09</td><td align=\"center\">3</td><td align=\"center\">1</td><td align=\"center\" style=\"color:#10b981;\">0.0320</td></tr>\n        </table>\n    </div>\n</div>",
        "state": {
            "query": "对比销售部与技术部差旅标准中，关于高铁与住宿的审批权限与金额上限差异",
            "complexity": "COMPLEX_COMPARISON",
            "target_departments": [
                "销售部",
                "技术部"
            ],
            "sub_queries": [
                {
                    "id": "sub_1",
                    "dept": "销售部",
                    "status": "DONE",
                    "chunks_count": 6
                },
                {
                    "id": "sub_2",
                    "dept": "技术部",
                    "status": "DONE",
                    "chunks_count": 6
                }
            ],
            "raw_chunks_count": 12,
            "retrieved_chunks": [
                {
                    "chunk_id": "sales_doc_c03",
                    "parent_id": "sales_doc_L2_01",
                    "level": 3,
                    "score": 0.0325,
                    "text": "高铁出行标准：销售经理及以上可乘一等座，其他人员二等座..."
                },
                {
                    "chunk_id": "sales_doc_c04",
                    "parent_id": "sales_doc_L2_01",
                    "level": 3,
                    "score": 0.0325,
                    "text": "住宿标准：一线城市800元/天，二线城市500元/天。审批人：部门总经理..."
                },
                {
                    "chunk_id": "tech_doc_c08",
                    "parent_id": "tech_doc_L2_03",
                    "level": 3,
                    "score": 0.032,
                    "text": "技术中心研发人员因公出差一律乘坐高铁二等座，特殊情况需VP审批..."
                },
                {
                    "chunk_id": "tech_doc_c09",
                    "parent_id": "tech_doc_L2_03",
                    "level": 3,
                    "score": 0.032,
                    "text": "住宿标准：一线城市600元/天，由直接项目组长审批后报HRBP备案..."
                }
            ]
        },
        "telemetry": [
            {
                "icon": "🔍",
                "title": "Milvus 2.5 混合检索完成",
                "status": "36ms",
                "desc": "Dense (BGE-M3): 18ms | Sparse (BM25): 14ms | RRF 融合: 4ms"
            },
            {
                "icon": "📈",
                "title": "召回有效率",
                "status": "100%",
                "desc": "两路扇出成功捕获 12 个高质量相关叶子切片"
            }
        ]
    },
    {
        "step": 4,
        "title": "Step 4: 上下文聚合与父级语义提升",
        "shortTitle": "4. 父级提升",
        "nodeName": "context_enricher",
        "nodeBadge": "Node: context_enricher",
        "nodeColor": "#f59e0b",
        "toolBadge": {
            "text": "Parent Chunk Enrichment",
            "bg": "rgba(245, 158, 11, 0.15)",
            "color": "#f59e0b"
        },
        "stateBadge": {
            "text": "De-duplicated & Lifted",
            "bg": "rgba(245, 158, 11, 0.15)",
            "color": "#f59e0b"
        },
        "cot": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\">\n        <strong style=\"color: #0f172a;\">核心痛点攻关：</strong>\n        <span style=\"color: #0284c7; font-weight: 600;\">L3 叶子块过于细碎，且可能截断报销表格！必须依据 parent_id 提升为完备 L2 块</span>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #f59e0b;\">\n        <div style=\"color: #f59e0b; font-size: 12.5px; font-weight: 700; margin-bottom: 8px;\">🧩 三级父子映射与去重聚合：</div>\n        <div style=\"font-size: 12px; color: #cbd5e1; line-height: 1.7;\">\n            <div>• <code>sales_doc_c03</code> &amp; <code>c04</code> 属于同一父块 <code>sales_doc_L2_01</code>（销售差旅规章第3章）</div>\n            <div>• <code>tech_doc_c08</code> &amp; <code>c09</code> 属于同一父块 <code>tech_doc_L2_03</code>（技术研发日常行政细则）</div>\n            <div>• <strong style=\"color: #10b981;\">提升效果：</strong>合并细碎碎片，从 12 个 L3 块精简提升为 4 个结构完备的 L2 语义块（包含完整的 Markdown 权限表格与特殊审批流程），彻底消除表格被拦腰切断的幻觉隐患！</div>\n        </div>\n    </div>\n</div>",
        "toolInfo": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\" style=\"margin-bottom: 10px;\">\n        <strong style=\"color: #0f172a;\">回查链路：</strong><code style=\"color: #7c3aed; font-weight: 600;\">Redis.mget(parent_ids) -&gt; PostgreSQL.query_chunks(fallback)</code>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #10b981;\">\n        <div style=\"color: #10b981; font-size: 12px; font-weight: 700; margin-bottom: 6px;\">⚡ 二级缓存命中表现：</div>\n        <div style=\"display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 11.5px; color: #cbd5e1;\">\n            <div>• Redis 缓存命中率：<span style=\"color:#10b981; font-weight:700;\">100% (4/4)</span></div>\n            <div>• 数据反序列化耗时：<span style=\"color:#f8fafc;\">3.2ms</span></div>\n            <div>• Markdown 表格保护：<span style=\"color:#10b981;\">完好保留 (2张完整矩阵)</span></div>\n            <div>• 候选块去重率：<span style=\"color:#38bdf8;\">66.7% (12 -&gt; 4)</span></div>\n        </div>\n    </div>\n</div>",
        "state": {
            "query": "对比销售部与技术部差旅标准中，关于高铁与住宿的审批权限与金额上限差异",
            "enriched_chunks": [
                {
                    "id": "sales_doc_L2_01",
                    "title": "销售部出差管理规定第三章：出行与住宿报销细则",
                    "type": "L2_TABLE_SECTION",
                    "length": 1420
                },
                {
                    "id": "sales_doc_L2_02",
                    "title": "销售部特批与超额报销流程",
                    "type": "L2_SECTION",
                    "length": 980
                },
                {
                    "id": "tech_doc_L2_03",
                    "title": "技术研发中心考勤与差旅规范§4",
                    "type": "L2_TABLE_SECTION",
                    "length": 1350
                },
                {
                    "id": "tech_doc_L2_04",
                    "title": "研发外派补贴与住宿标准补充说明",
                    "type": "L2_SECTION",
                    "length": 860
                }
            ],
            "lifted_from_l3_count": 12,
            "final_l2_count": 4
        },
        "telemetry": [
            {
                "icon": "🚀",
                "title": "父级语义块提升完成",
                "status": "8ms",
                "desc": "100% Redis 缓存直接命中，无 PG 磁盘穿透"
            },
            {
                "icon": "✨",
                "title": "结构完整性校验",
                "status": "PASS",
                "desc": "父级 Markdown 表格语法闭合，上下文无缺失"
            }
        ]
    },
    {
        "step": 5,
        "title": "Step 5: Qwen Reranker 精排打分与容灾",
        "shortTitle": "5. 精排打分",
        "nodeName": "rerank_evaluator",
        "nodeBadge": "Node: rerank_evaluator",
        "nodeColor": "#ef4444",
        "toolBadge": {
            "text": "Qwen Reranker",
            "bg": "rgba(239, 68, 68, 0.15)",
            "color": "#ef4444"
        },
        "stateBadge": {
            "text": "Top-2 Selected",
            "bg": "rgba(239, 68, 68, 0.15)",
            "color": "#ef4444"
        },
        "cot": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\">\n        <strong style=\"color: #0f172a;\">精排机制：</strong>\n        <span style=\"color: #0284c7; font-weight: 600;\">Cross-Encoder 深度注意力交叉打分，过滤边缘噪声块，保留 Top-2 高置信证据</span>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #ef4444;\">\n        <div style=\"color: #ef4444; font-size: 12.5px; font-weight: 700; margin-bottom: 8px;\">⚖️ Qwen Reranker 打分与熔断状态：</div>\n        <div style=\"font-size: 12px; color: #cbd5e1; line-height: 1.7;\">\n            <div>• <code>sales_doc_L2_01</code>：相关性分数 <strong style=\"color:#10b981;\">0.892</strong>（包含完整高铁席别、800元住宿及总经理审批流）</div>\n            <div>• <code>tech_doc_L2_03</code>：相关性分数 <strong style=\"color:#10b981;\">0.887</strong>（包含完整高铁二等座限制、600元住宿及组长/HRBP审批流）</div>\n            <div>• <code>sales_doc_L2_02</code> (0.312) &amp; <code>tech_doc_L2_04</code> (0.285)：低于阈值 0.45，判定为边缘补充，自动剔除。</div>\n            <div style=\"color: #10b981; margin-top: 4px;\">🛡️ 90s 超时监控保护正常：响应时间 142ms，远在熔断阈值内，无需降级。</div>\n        </div>\n    </div>\n</div>",
        "toolInfo": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\" style=\"margin-bottom: 10px;\">\n        <strong style=\"color: #0f172a;\">模型推理：</strong><code style=\"color: #7c3aed; font-weight: 600;\">Qwen-Reranker-7B.score(query, [chunk1, chunk2, chunk3, chunk4])</code>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #38bdf8;\">\n        <div style=\"color: #38bdf8; font-size: 12px; font-weight: 700; margin-bottom: 6px;\">🎯 精排后证据金字塔：</div>\n        <div style=\"font-size: 11.5px; color: #cbd5e1; line-height: 1.6;\">\n            1. [0.892] <strong>销售部差旅规范§3</strong> (包含两项完整审批权限及限额)<br>\n            2. [0.887] <strong>技术部差旅细则§4</strong> (包含两项完整审批权限及限额)<br>\n            <span style=\"color: #94a3b8;\">------------------ 阈值线 0.45 (截断) ------------------</span><br>\n            <span style=\"color: #64748b;\">3. [0.312] 销售部特批说明 (丢弃) | 4. [0.285] 研发外派补贴 (丢弃)</span>\n        </div>\n    </div>\n</div>",
        "state": {
            "query": "对比销售部与技术部差旅标准中，关于高铁与住宿的审批权限与金额上限差异",
            "reranked_chunks": [
                {
                    "id": "sales_doc_L2_01",
                    "score": 0.892,
                    "accepted": true,
                    "source": "销售部出差管理规定v2.1"
                },
                {
                    "id": "tech_doc_L2_03",
                    "score": 0.887,
                    "accepted": true,
                    "source": "技术中心日常行政细则v1.4"
                }
            ],
            "dropped_count": 2,
            "degradation_triggered": false
        },
        "telemetry": [
            {
                "icon": "⚖️",
                "title": "Qwen Reranker 精排完毕",
                "status": "142ms",
                "desc": "打分耗时正常 | 截断率 50% | 保留 Top-2 优质块"
            },
            {
                "icon": "🛡️",
                "title": "高可用容灾守卫",
                "status": "ACTIVE",
                "desc": "90s 超时熔断器处于健康监听状态，未触发降级"
            }
        ]
    },
    {
        "step": 6,
        "title": "Step 6: 证据充分性评分 (EvidenceGrade)",
        "shortTitle": "6. 证据评分",
        "nodeName": "evidence_grader",
        "nodeBadge": "Node: evidence_grader",
        "nodeColor": "#ec4899",
        "toolBadge": {
            "text": "Pydantic Grade Check",
            "bg": "rgba(236, 72, 153, 0.15)",
            "color": "#ec4899"
        },
        "stateBadge": {
            "text": "Evidence Validated",
            "bg": "rgba(16, 185, 129, 0.15)",
            "color": "#10b981"
        },
        "cot": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\">\n        <strong style=\"color: #0f172a;\">质量门禁判断：</strong>\n        <span style=\"color: #0284c7; font-weight: 600;\">验证 Top-2 证据块是否能完整回答用户提出的全部 4 个对比槽位</span>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #ec4899;\">\n        <div style=\"color: #ec4899; font-size: 12.5px; font-weight: 700; margin-bottom: 8px;\">🔬 槽位完整性核对矩阵：</div>\n        <ul style=\"margin: 0; padding-left: 18px; font-size: 12px; color: #cbd5e1; line-height: 1.7;\">\n            <li>[槽位 1] 销售部高铁席别与审批权限：<span style=\"color:#10b981;\">已覆盖 (经理以上一等座，总监审批)</span></li>\n            <li>[槽位 2] 销售部住宿限额与审批权限：<span style=\"color:#10b981;\">已覆盖 (800元/天，总监+财务审批)</span></li>\n            <li>[槽位 3] 技术部高铁席别与审批权限：<span style=\"color:#10b981;\">已覆盖 (二等座，特殊需VP审批)</span></li>\n            <li>[槽位 4] 技术部住宿限额与审批权限：<span style=\"color:#10b981;\">已覆盖 (600元/天，组长审批+HRBP备案)</span></li>\n        </ul>\n        <div style=\"margin-top: 6px; font-size: 12px; color: #10b981; font-weight: 600;\">\n            ✓ 裁决：relevant=True, sufficient=True, ambiguous=False。直接放行至 synthesizer！无需 HyDE/Step-back 改写！\n        </div>\n    </div>\n</div>",
        "toolInfo": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\" style=\"margin-bottom: 10px;\">\n        <strong style=\"color: #0f172a;\">结构化评估：</strong><code style=\"color: #7c3aed; font-weight: 600;\">EvidenceGrader.validate(EvidenceGrade)</code>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #10b981;\">\n        <div style=\"color: #10b981; font-size: 12px; font-weight: 700; margin-bottom: 6px;\">📋 EvidenceGrade Pydantic 校验实例：</div>\n        <pre style=\"margin:0; font-size: 11px; color: #38bdf8; font-family: monospace;\">{\n  \"relevant\": true,\n  \"sufficient\": true,\n  \"ambiguous\": false,\n  \"missing_slots\": [],\n  \"confidence_score\": 0.96,\n  \"next_action\": \"synthesizer\"\n}</pre>\n    </div>\n</div>",
        "state": {
            "query": "对比销售部与技术部差旅标准中，关于高铁与住宿的审批权限与金额上限差异",
            "evidence_grade": {
                "relevant": true,
                "sufficient": true,
                "ambiguous": false,
                "missing_slots": [],
                "confidence_score": 0.96
            },
            "rewrite_count": 0,
            "next_node": "synthesizer"
        },
        "telemetry": [
            {
                "icon": "✅",
                "title": "证据门禁质检通过",
                "status": "PASS",
                "desc": "耗时 98ms | 4 个关键信息槽位 100% 完整匹配"
            },
            {
                "icon": "🛡️",
                "title": "避免无效循环",
                "status": "BYPASS_REWRITE",
                "desc": "证据完备，直接跳过改写阶段，降低响应耗时"
            }
        ]
    },
    {
        "step": 7,
        "title": "Step 7: 约束合成与事实溯源标注",
        "shortTitle": "7. 约束合成",
        "nodeName": "synthesizer",
        "nodeBadge": "Node: synthesizer",
        "nodeColor": "#38bdf8",
        "toolBadge": {
            "text": "Strict Fact Synthesis",
            "bg": "rgba(56, 189, 248, 0.15)",
            "color": "#38bdf8"
        },
        "stateBadge": {
            "text": "Streaming Answer",
            "bg": "rgba(56, 189, 248, 0.15)",
            "color": "#38bdf8"
        },
        "cot": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\">\n        <strong style=\"color: #0f172a;\">合成要求：</strong>\n        <span style=\"color: #0284c7; font-weight: 600;\">100% 忠实于上下文；使用结构化 Markdown 对比表格；每条结论必须附带 [1] / [2] 溯源角标</span>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #38bdf8;\">\n        <div style=\"color: #38bdf8; font-size: 12.5px; font-weight: 700; margin-bottom: 8px;\">📝 生成的对比表格预览：</div>\n        <table style=\"width: 100%; font-size: 11.5px; color: #cbd5e1; border-collapse: collapse;\">\n            <tr style=\"border-bottom: 1px solid rgba(255,255,255,0.15); color: #94a3b8;\">\n                <th style=\"text-align: left; padding: 4px 0;\">维度</th><th>销售部标准 [1]</th><th>技术部标准 [2]</th><th>主要差异说明</th>\n            </tr>\n            <tr>\n                <td style=\"color:#f8fafc; font-weight:600;\">高铁席别</td>\n                <td>经理及以上一等座，其余二等座</td>\n                <td>全员默认二等座</td>\n                <td>销售部按职级区分，技术部全员平权</td>\n            </tr>\n            <tr>\n                <td style=\"color:#f8fafc; font-weight:600;\">住宿上限</td>\n                <td>一线800元/天，二线500元/天</td>\n                <td>一线600元/天，二线400元/天</td>\n                <td>销售部上限高出 200元/天 (业务外勤需要)</td>\n            </tr>\n            <tr>\n                <td style=\"color:#f8fafc; font-weight:600;\">审批权限</td>\n                <td>部门总经理 + 财务部总监</td>\n                <td>直属组长审批 + HRBP备案</td>\n                <td>技术部审批链更扁平，销售部强调财务风控</td>\n            </tr>\n        </table>\n    </div>\n</div>",
        "toolInfo": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\" style=\"margin-bottom: 10px;\">\n        <strong style=\"color: #0f172a;\">流式通道：</strong><code style=\"color: #7c3aed; font-weight: 600;\">SSEGenerator.push(event=\"token\", data={\"delta\": text})</code>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #10b981;\">\n        <div style=\"color: #10b981; font-size: 12px; font-weight: 700; margin-bottom: 6px;\">📚 溯源引用锚点挂载表：</div>\n        <div style=\"font-size: 11.5px; color: #cbd5e1; line-height: 1.6;\">\n            • <code>[1]</code> 销售部出差管理规定v2.1 §3.2 (DocID: <code>sales_doc_L2_01</code>)<br>\n            • <code>[2]</code> 技术中心日常行政细则v1.4 §4.1 (DocID: <code>tech_doc_L2_03</code>)\n        </div>\n    </div>\n</div>",
        "state": {
            "query": "对比销售部与技术部差旅标准中，关于高铁与住宿的审批权限与金额上限差异",
            "synthesized_answer": "### 销售部与技术部差旅标准对比分析\n\n依据企业最新规章制度，两部门在高铁与住宿方面的对比如下：...",
            "citations": [
                {
                    "index": 1,
                    "doc_name": "销售部出差管理规定v2.1",
                    "section": "§3.2 报销上限与审批",
                    "chunk_id": "sales_doc_L2_01"
                },
                {
                    "index": 2,
                    "doc_name": "技术中心日常行政细则v1.4",
                    "section": "§4.1 差旅规范",
                    "chunk_id": "tech_doc_L2_03"
                }
            ],
            "generation_tokens": 468
        },
        "telemetry": [
            {
                "icon": "⚡",
                "title": "首 Token 响应延迟 (TTFT)",
                "status": "340ms",
                "desc": "FastModel 预热与高效 Prompt 流式切片推送"
            },
            {
                "icon": "📊",
                "title": "生成速度",
                "status": "48 tok/s",
                "desc": "输出 468 tokens | 溯源角标 100% 匹配"
            }
        ]
    },
    {
        "step": 8,
        "title": "Step 8: SSE 完整推送与审计落库",
        "shortTitle": "8. 推送与审计",
        "nodeName": "session_ledger",
        "nodeBadge": "Node: session_ledger",
        "nodeColor": "#10b981",
        "toolBadge": {
            "text": "SSE Dispatch & Audit",
            "bg": "rgba(16, 185, 129, 0.15)",
            "color": "#10b981"
        },
        "stateBadge": {
            "text": "Trace Persisted",
            "bg": "rgba(16, 185, 129, 0.15)",
            "color": "#10b981"
        },
        "cot": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\">\n        <strong style=\"color: #0f172a;\">生命周期收尾：</strong>\n        <span style=\"color: #0284c7; font-weight: 600;\">向客户端推送完成事件，将本次执行完整 Trace、引用快照及用户消息持久化</span>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #10b981;\">\n        <div style=\"color: #10b981; font-size: 12.5px; font-weight: 700; margin-bottom: 8px;\">📦 全链路审计账本落库完成：</div>\n        <ul style=\"margin: 0; padding-left: 18px; font-size: 12px; color: #cbd5e1; line-height: 1.7;\">\n            <li><strong style=\"color:#f8fafc;\">Session ID：</strong><code>sess_9a82f1b4_20260924</code></li>\n            <li><strong style=\"color:#f8fafc;\">PostgreSQL 持久化：</strong>更新 <code>chat_messages</code> 与 <code>rag_traces</code> 表，记录 8 步完整快照</li>\n            <li><strong style=\"color:#f8fafc;\">Redis 会话滑窗：</strong>更新该会话近 10 轮交互上下文缓存</li>\n            <li><strong style=\"color:#f8fafc;\">不可篡改审计散列：</strong><code>sha256:d82f710a3e...b41</code></li>\n        </ul>\n    </div>\n</div>",
        "toolInfo": "<div style=\"line-height: 1.6;\">\n    <div class=\"trace-query-box\" style=\"margin-bottom: 10px;\">\n        <strong style=\"color: #0f172a;\">SSE 终结协议：</strong><code style=\"color: #7c3aed; font-weight: 600;\">push(event=\"done\", data={\"trace_id\": \"tr_8f91a\", \"total_latency\": 921})</code>\n    </div>\n    <div class=\"trace-terminal-card\" style=\"border-left: 4px solid #38bdf8;\">\n        <div style=\"color: #38bdf8; font-size: 12px; font-weight: 700; margin-bottom: 6px;\">📈 全链路性能总账单：</div>\n        <div style=\"display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 11.5px; color: #cbd5e1;\">\n            <div>• 端到端总耗时：<span style=\"color:#10b981; font-weight:700;\">921ms</span></div>\n            <div>• 召回候选块数：<span style=\"color:#f8fafc;\">12 块 (L3)</span></div>\n            <div>• 父级提升后块数：<span style=\"color:#f8fafc;\">4 块 (L2)</span></div>\n            <div>• 最终采纳证据：<span style=\"color:#10b981; font-weight:700;\">2 块 (100% 命中)</span></div>\n            <div>• 改写轮数：<span style=\"color:#10b981;\">0 轮 (直接命中)</span></div>\n            <div>• 异常与降级：<span style=\"color:#10b981;\">0 次 (全流程平稳)</span></div>\n        </div>\n    </div>\n</div>",
        "state": {
            "query": "对比销售部与技术部差旅标准中，关于高铁与住宿的审批权限与金额上限差异",
            "session_id": "sess_9a82f1b4_20260924",
            "trace_id": "tr_8f91a_prod",
            "status": "COMPLETED",
            "total_latency_ms": 921,
            "audit_hash": "sha256:d82f710a3e819cd201bfa8294d"
        },
        "telemetry": [
            {
                "icon": "🏁",
                "title": "端到端生命周期圆满完成",
                "status": "SUCCESS",
                "desc": "总耗时 921ms | 全过程 8 步 Trace 审计完成"
            },
            {
                "icon": "💾",
                "title": "PostgreSQL & Redis 账本落库",
                "status": "PERSISTED",
                "desc": "会话数据与溯源快照已双写至持久层"
            }
        ]
    }
];

let traceCurrentStep = 0;
let traceIsPlaying = false;
let tracePlayTimer = null;
let traceSpeed = 2000;

function initTraceStepper() {
    const container = document.getElementById("trace-stepper-container");
    if (!container) return;
    let html = "";
    TRACE_STEPS.forEach((st, idx) => {
        html += `
            <div class="trace-step-indicator ${idx === 0 ? 'active' : ''}" id="trace-step-ind-${idx}" onclick="traceSetStep(${idx})">
                <div class="trace-step-number">${idx + 1}</div>
                <div class="trace-step-label">${st.shortTitle}</div>
            </div>
        `;
    });
    container.innerHTML = html;
}

function traceSetStep(idx) {
    if (idx < 0 || idx >= TRACE_STEPS.length) return;
    traceCurrentStep = idx;
    const stepData = TRACE_STEPS[idx];

    // 更新 Stepper 指示器高亮
    document.querySelectorAll('.trace-step-indicator').forEach((el, i) => {
        if (i === idx) {
            el.classList.add('active');
        } else {
            el.classList.remove('active');
        }
    });

    // 更新进度条
    const progressBar = document.getElementById("trace-progress-bar");
    if (progressBar) {
        const pct = ((idx + 1) / TRACE_STEPS.length) * 100;
        progressBar.style.width = pct + '%';
    }

    // 更新 Panel 1: CoT & Intent
    const cotBody = document.getElementById("trace-cot-body");
    const nodeBadge = document.getElementById("trace-node-badge");
    if (cotBody) cotBody.innerHTML = stepData.cot;
    if (nodeBadge) {
        nodeBadge.innerText = stepData.nodeBadge;
        nodeBadge.style.background = stepData.toolBadge.bg;
        nodeBadge.style.color = stepData.nodeColor;
    }

    // 更新 Panel 2: Tool Dispatch
    const toolBody = document.getElementById("trace-tool-body");
    const toolBadge = document.getElementById("trace-tool-badge");
    if (toolBody) toolBody.innerHTML = stepData.toolInfo;
    if (toolBadge) {
        toolBadge.innerText = stepData.toolBadge.text;
        toolBadge.style.background = stepData.toolBadge.bg;
        toolBadge.style.color = stepData.toolBadge.color;
    }

    // 更新 Panel 3: State JSON
    const stateJson = document.getElementById("trace-state-json");
    const stateBadge = document.getElementById("trace-state-badge");
    if (stateJson) {
        stateJson.innerText = JSON.stringify(stepData.state, null, 2);
    }
    if (stateBadge) {
        stateBadge.innerText = stepData.stateBadge.text;
        stateBadge.style.background = stepData.stateBadge.bg;
        stateBadge.style.color = stepData.stateBadge.color;
    }

    // 更新 Panel 4: Telemetry
    const telemetryBody = document.getElementById("trace-telemetry-body");
    const latencyBadge = document.getElementById("trace-latency-badge");
    if (telemetryBody) {
        let html = '';
        stepData.telemetry.forEach(item => {
            html += `
                <div class="trace-telemetry-item">
                    <div class="trace-telemetry-icon">${item.icon}</div>
                    <div class="trace-telemetry-body">
                        <div class="trace-telemetry-title">
                            <span>${item.title}</span>
                            <span class="pill" style="font-size:11px; background: rgba(56, 189, 248, 0.15); color: #38bdf8;">${item.status}</span>
                        </div>
                        <div class="trace-telemetry-desc">${item.desc}</div>
                    </div>
                </div>
            `;
        });
        telemetryBody.innerHTML = html;
    }
}

function traceNext() {
    if (traceCurrentStep < TRACE_STEPS.length - 1) {
        traceSetStep(traceCurrentStep + 1);
    } else {
        if (traceIsPlaying) traceTogglePlay();
    }
}

function tracePrev() {
    if (traceCurrentStep > 0) {
        traceSetStep(traceCurrentStep - 1);
    }
}

function traceReset() {
    if (traceIsPlaying) traceTogglePlay();
    traceSetStep(0);
}

function traceTogglePlay() {
    const btn = document.getElementById("trace-btn-play");
    const textSpan = document.getElementById("trace-play-text");
    if (traceIsPlaying) {
        clearInterval(tracePlayTimer);
        traceIsPlaying = false;
        if (textSpan) textSpan.innerText = "▶ 自动播放";
        if (btn) btn.classList.replace("trace-btn-secondary", "trace-btn-primary");
    } else {
        if (traceCurrentStep >= TRACE_STEPS.length - 1) {
            traceSetStep(0);
        }
        traceIsPlaying = true;
        if (textSpan) textSpan.innerText = "⏸ 暂停";
        if (btn) btn.classList.replace("trace-btn-primary", "trace-btn-secondary");

        tracePlayTimer = setInterval(() => {
            if (traceCurrentStep < TRACE_STEPS.length - 1) {
                traceNext();
            } else {
                traceTogglePlay();
            }
        }, traceSpeed);
    }
}

function traceSetSpeed(speedVal) {
    traceSpeed = parseInt(speedVal, 10);
    if (traceIsPlaying) {
        clearInterval(tracePlayTimer);
        tracePlayTimer = setInterval(() => {
            if (traceCurrentStep < TRACE_STEPS.length - 1) {
                traceNext();
            } else {
                traceTogglePlay();
            }
        }, traceSpeed);
    }
}

// 页面加载完成初始化
document.addEventListener("DOMContentLoaded", function() {
    if (window.mermaid) {
        mermaid.initialize({ startOnLoad: false, theme: 'neutral', securityLevel: 'loose' });
        renderVisibleMermaid();
    }
    initTraceStepper();
    traceSetStep(0);
});
"""
