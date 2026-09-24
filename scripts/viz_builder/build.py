# -*- coding: utf-8 -*-
"""Builder for SuperMew Visualization (24-page enterprise full edition)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import styles
import header_sidebar
import scripts_js

def build(output_path="../../supermew_visualization.html"):
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
    <title>SuperMew 企业级受控 RAG 问答系统架构与核心源码全景解析</title>
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
</body>
</html>
"""
    with open(resolved_path, "w", encoding="utf-8") as f:
        f.write(html)
    size_kb = os.path.getsize(resolved_path) / 1024
    print(f"Successfully generated {resolved_path} ({size_kb:.2f} KB)")

if __name__ == "__main__":
    build()
