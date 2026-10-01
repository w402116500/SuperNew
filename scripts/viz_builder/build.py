# -*- coding: utf-8 -*-
"""Builder for RagTrail Visualization (24-page enterprise full edition)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import styles
import header_sidebar
import scripts_js

def build(output_path="../../ragtrail_visualization.html"):
    base_dir = os.path.dirname(__file__)
    resolved_path = os.path.abspath(os.path.join(base_dir, output_path))

    # Read the 5 HTML sections
    sections = [
        "section_overview.html",
        "section_ingestion.html",
        "section_retrieval.html",
        "section_runtime.html",
        "section_source.html"
    ]
    main_content_parts = []
    for sec in sections:
        sec_path = os.path.join(base_dir, sec)
        with open(sec_path, "r", encoding="utf-8") as f:
            main_content_parts.append(f.read())
            
    main_content_html = "\n\n".join(main_content_parts)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RagTrail 企业级受控 RAG 问答系统架构与核心源码全景解析</title>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <style>
{styles.CSS_CONTENT}
    </style>
</head>
<body>
{header_sidebar.HEADER_HTML}

    <div class="container">
{header_sidebar.SIDEBAR_HTML}

        <div class="main-content">
{main_content_html}
        </div>
    </div>

    <script>
{scripts_js.JS_CONTENT}
    </script>

    <!-- 全屏图表/图片灯箱查看器 (Diagram & Image Lightbox Modal) -->
    <div id="diagram-lightbox-modal" class="lightbox-modal" style="display: none;" aria-hidden="true">
        <div class="lightbox-backdrop" onclick="closeLightbox()"></div>
        <div class="lightbox-header">
            <div class="lightbox-title-area">
                <span class="lightbox-icon">🔍</span>
                <span id="lightbox-title" class="lightbox-title">全景架构图全屏查看</span>
            </div>
            <div class="lightbox-actions">
                <button type="button" class="lightbox-btn" onclick="toggleLightboxFullscreen()" title="切换全屏模式">
                    <span class="btn-icon">⛶</span>
                    <span class="btn-text">全屏</span>
                </button>
                <button type="button" class="lightbox-btn lightbox-btn-close" onclick="closeLightbox()" title="关闭 (Esc)">
                    <span class="btn-icon">✕</span>
                    <span class="btn-text">关闭</span>
                </button>
            </div>
        </div>
        <div class="lightbox-viewport" id="lightbox-viewport">
            <div class="lightbox-stage" id="lightbox-stage">
                <div class="lightbox-stage-canvas" id="lightbox-stage-canvas">
                    <!-- 动态克隆的 SVG 或 IMG -->
                </div>
            </div>
        </div>
        <div class="lightbox-toolbar">
            <button type="button" class="tb-btn" onclick="zoomLightbox(-0.25)" title="缩小 (-)">
                <span>➖</span>
            </button>
            <span id="lightbox-zoom-display" class="tb-zoom-text" onclick="resetLightboxZoom()" title="点击重置为 100%">100%</span>
            <button type="button" class="tb-btn" onclick="zoomLightbox(0.25)" title="放大 (+)">
                <span>➕</span>
            </button>
            <div class="tb-divider"></div>
            <button type="button" class="tb-btn" onclick="fitLightbox()" title="自适应窗口大小">
                <span>🎯</span>
                <span class="tb-label">适应</span>
            </button>
            <button type="button" class="tb-btn" onclick="resetLightboxZoom()" title="还原原始比例 (1:1)">
                <span>1:1</span>
            </button>
            <button type="button" class="tb-btn" onclick="downloadLightboxContent()" title="保存导出为矢量图/图片">
                <span>💾</span>
                <span class="tb-label">导出</span>
            </button>
        </div>
        <div class="lightbox-hint">
            <span>💡 提示：按住鼠标左键可拖拽平移 · 滚轮可缩放 · 按 Esc 退出</span>
        </div>
    </div>

</body>
</html>
"""
    with open(resolved_path, "w", encoding="utf-8") as f:
        f.write(html)
    size_kb = os.path.getsize(resolved_path) / 1024
    print(f"Successfully generated {resolved_path} ({size_kb:.2f} KB)")

if __name__ == "__main__":
    build()
