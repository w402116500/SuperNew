# -*- coding: utf-8 -*-
"""Header and Sidebar definitions for SuperMew Visualization (24-page enterprise full edition)."""

HEADER_HTML = """
    <header class="header">
        <div class="brand">
            <button class="mobile-menu-btn" id="mobile-menu-toggle" onclick="toggleMobileSidebar()" aria-label="打开导航菜单">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                    <line x1="3" y1="12" x2="21" y2="12"></line>
                    <line x1="3" y1="6" x2="21" y2="6"></line>
                    <line x1="3" y1="18" x2="21" y2="18"></line>
                </svg>
            </button>
            <div class="brand-icon">S</div>
            <div class="brand-text">
                <h1>SuperMew <span class="header-tag">企业级受控 RAG 问答系统</span></h1>
                <p>L1/L2/L3 三级父子分块 · BGE-M3 + Milvus 2.5 原生 BM25 混合检索 · LangGraph 复杂规划 · 300 题评测闭环</p>
            </div>
        </div>
        <div class="header-badges">
            <span class="tag-badge tag-purple">LangGraph 复杂规划</span>
            <span class="tag-badge tag-blue">L1/L2/L3 父子分块</span>
            <span class="tag-badge tag-green">Milvus 2.5 原生 BM25</span>
            <span class="tag-badge tag-purple">Qwen Reranker 精排</span>
            <span class="tag-badge tag-amber">SSE 全景 Trace</span>
            <span class="tag-badge tag-blue">300 题评测 +22 题</span>
        </div>
    </header>
"""

SIDEBAR_HTML = """
        <div class="sidebar-backdrop" id="sidebar-backdrop" onclick="closeMobileSidebar()"></div>
        <div class="sidebar">
            <div class="tree-group">
                <div class="group-title">核心概览</div>
                <div class="tree-item">
                    <div class="file active" id="nav-overview" onclick="navigatePage('page-overview')">
                        <span class="file-title">🌐 项目架构全景概览</span>
                        <span class="pill">Overview</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-boundaries" onclick="navigatePage('page-boundaries')">
                        <span class="file-title">🔭 跨层数据流与事实所有权</span>
                        <span class="pill">Flow</span>
                    </div>
                </div>
            </div>

            <div class="tree-group">
                <div class="group-title">全链路架构与运行机制</div>
                <div class="tree-item">
                    <div class="file" id="nav-code-map" onclick="navigatePage('page-code-map')">
                        <span class="file-title">🗺️ 项目代码地图与职责边界</span>
                        <span class="pill">Map</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-ingestion-lifecycle" onclick="navigatePage('page-ingestion-lifecycle')">
                        <span class="file-title">📥 多格式入库与原子暂存事务</span>
                        <span class="pill">Ingest</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-mineru-pipeline" onclick="navigatePage('page-mineru-pipeline')">
                        <span class="file-title">📑 MinerU 多模态解析与跨页还原</span>
                        <span class="pill">MinerU</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-hierarchical-chunking" onclick="navigatePage('page-hierarchical-chunking')">
                        <span class="file-title">🧩 L1/L2/L3 分块与 AST 保护</span>
                        <span class="pill">Chunk</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-retrieval-hybrid" onclick="navigatePage('page-retrieval-hybrid')">
                        <span class="file-title">🔎 混合检索、RRF 与上下文恢复</span>
                        <span class="pill">Retrieve</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-rerank-degradation" onclick="navigatePage('page-rerank-degradation')">
                        <span class="file-title">⚖️ Qwen Reranker 精排与降级</span>
                        <span class="pill">Rerank</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-langgraph-orchestrator" onclick="navigatePage('page-langgraph-orchestrator')">
                        <span class="file-title">⚙️ LangGraph 多 Agent 规划状态机</span>
                        <span class="pill">Graph</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-live-trace" onclick="navigatePage('page-live-trace')">
                        <span class="file-title">🎬 复杂问题端到端 Live Trace 演示</span>
                        <span class="pill pill-purple">Demo</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-evidence-grader" onclick="navigatePage('page-evidence-grader')">
                        <span class="file-title">🔬 EvidenceGrade 评分与改写</span>
                        <span class="pill">Grade</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-security-guardrails" onclick="navigatePage('page-security-guardrails')">
                        <span class="file-title">🛡️ 安全审计、防注入与幻觉拒答防线</span>
                        <span class="pill">Guard</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-sse-events" onclick="navigatePage('page-sse-events')">
                        <span class="file-title">📡 SSE 流式协议与全景 Trace</span>
                        <span class="pill">SSE</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-session-storage" onclick="navigatePage('page-session-storage')">
                        <span class="file-title">🧠 会话隔离、PG/Redis 与 HITL</span>
                        <span class="pill">Store</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-frontend-stream" onclick="navigatePage('page-frontend-stream')">
                        <span class="file-title">💻 Vue 3 前端流式渲染与引用悬浮</span>
                        <span class="pill">Frontend</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-evaluation-benchmark" onclick="navigatePage('page-evaluation-benchmark')">
                        <span class="file-title">📊 300 题可复现评测与归因</span>
                        <span class="pill pill-green">Eval</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-evaluation-runner" onclick="navigatePage('page-evaluation-runner')">
                        <span class="file-title">🧪 196KB 评测执行器与判定流水线</span>
                        <span class="pill">Runner</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-contracts-api" onclick="navigatePage('page-contracts-api')">
                        <span class="file-title">📐 Contracts 与全链路 API 契约</span>
                        <span class="pill">Wire</span>
                    </div>
                </div>
            </div>

            <div class="tree-group">
                <div class="group-title">核心源码深入解析</div>
                <div class="tree-item">
                    <div class="file" id="nav-src-rag-pipeline" onclick="navigatePage('page-src-rag-pipeline')">
                        <span class="file-title">📜 pipeline.py (编排图与扇出)</span>
                        <span class="pill">LangGraph</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-src-rag-utils" onclick="navigatePage('page-src-rag-utils')">
                        <span class="file-title">📜 utils.py (混合检索与精排)</span>
                        <span class="pill">Hybrid</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-src-ingestion" onclick="navigatePage('page-src-ingestion')">
                        <span class="file-title">📜 ingestion & semantic_chunking.py</span>
                        <span class="pill">Ingest</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-src-milvus" onclick="navigatePage('page-src-milvus')">
                        <span class="file-title">📜 milvus_writer.py (Milvus 2.5)</span>
                        <span class="pill">BM25</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-src-evaluation-runner" onclick="navigatePage('page-src-evaluation-runner')">
                        <span class="file-title">📜 runner.py (300 题评测自动化引擎)</span>
                        <span class="pill">EvalEngine</span>
                    </div>
                </div>
                <div class="tree-item">
                    <div class="file" id="nav-src-chat-runtime" onclick="navigatePage('page-src-chat-runtime')">
                        <span class="file-title">📜 runtime.py & service.py (Agent/SSE)</span>
                        <span class="pill">Agent/SSE</span>
                    </div>
                </div>
            </div>
        </div>
"""
