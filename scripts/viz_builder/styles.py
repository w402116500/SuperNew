# -*- coding: utf-8 -*-
"""CSS Styles for SuperMew Visualization Dashboard (Enterprise Responsive Edition)."""

CSS_CONTENT = """
        :root {
            --primary-color: #0284c7;
            --primary-light: #e0f2fe;
            --primary-dark: #0369a1;
            --secondary-color: #6366f1;
            --secondary-light: #eef2ff;
            --accent-color: #10b981;
            --accent-light: #ecfdf5;
            --warning-color: #f59e0b;
            --warning-light: #fffbeb;
            --danger-color: #ef4444;
            --danger-light: #fef2f2;
            --bg-color: #f8fafc;
            --text-color: #0f172a;
            --text-muted: #64748b;
            --card-bg: #ffffff;
            --border-color: #e2e8f0;
            --code-bg: #0f172a;
            --sidebar-width: 320px;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            display: flex;
            flex-direction: column;
            height: 100vh;
            overflow: hidden;
        }

        /* 顶部 Header */
        header, .header {
            background-color: var(--card-bg);
            padding: 12px 28px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
            display: flex;
            align-items: center;
            justify-content: space-between;
            z-index: 20;
            border-bottom: 1px solid var(--border-color);
            flex-shrink: 0;
            height: 64px;
        }

        .brand, .header-left {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .mobile-menu-btn {
            display: none;
            align-items: center;
            justify-content: center;
            width: 38px;
            height: 38px;
            border-radius: 8px;
            background: #f8fafc;
            border: 1px solid var(--border-color);
            color: var(--text-color);
            cursor: pointer;
            transition: all 0.2s;
            flex-shrink: 0;
        }

        .mobile-menu-btn:hover {
            background: var(--primary-light);
            color: var(--primary-color);
        }

        .brand-icon, .logo {
            width: 38px;
            height: 38px;
            background: linear-gradient(135deg, var(--primary-color), var(--secondary-color));
            color: white;
            border-radius: 9px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            font-size: 20px;
            box-shadow: 0 2px 5px rgba(2,132,199,0.25);
            flex-shrink: 0;
            user-select: none;
        }

        .brand-text, .title-row {
            display: flex;
            flex-direction: column;
            justify-content: center;
        }

        .brand-text h1, .title-row .title {
            margin: 0;
            font-size: 18px;
            color: #0f172a;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
            line-height: 1.2;
        }

        .header-tag, .brand-text h1 span, .title-row .badge {
            color: var(--primary-color);
            font-size: 13px;
            font-weight: 600;
            background: var(--primary-light);
            padding: 2px 8px;
            border-radius: 6px;
            border: 1px solid #bae6fd;
            white-space: nowrap;
        }

        .brand-text p, .subtitle {
            margin: 3px 0 0 0;
            font-size: 11.5px;
            color: var(--text-muted);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 620px;
        }

        .header-badges, .header-right {
            display: flex;
            gap: 7px;
            align-items: center;
            flex-wrap: wrap;
        }

        .tag-badge, .header-right .badge {
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 11.5px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 5px;
            white-space: nowrap;
        }

        .tag-blue, .badge-blue { background: var(--primary-light); color: var(--primary-dark); border: 1px solid #bae6fd; }
        .tag-indigo, .badge-indigo { background: var(--secondary-light); color: var(--secondary-color); border: 1px solid #c7d2fe; }
        .tag-green, .badge-green { background: var(--accent-light); color: #047857; border: 1px solid #a7f3d0; }
        .tag-amber, .badge-amber { background: var(--warning-light); color: #b45309; border: 1px solid #fde68a; }
        .tag-purple, .badge-purple { background: #faf5ff; color: #7c3aed; border: 1px solid #e9d5ff; }
        .tag-danger, .badge-danger { background: var(--danger-light); color: var(--danger-color); border: 1px solid #fecaca; }

        /* 总体布局容器 */
        .container {
            display: flex;
            flex: 1;
            overflow: hidden;
            position: relative;
        }

        /* 移动端侧边栏遮罩 */
        .sidebar-backdrop {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(15, 23, 42, 0.45);
            backdrop-filter: blur(2px);
            z-index: 90;
            transition: opacity 0.25s ease;
        }

        /* 侧边栏：项目模块导航 */
        .sidebar {
            width: var(--sidebar-width);
            background-color: var(--card-bg);
            border-right: 1px solid var(--border-color);
            overflow-y: auto;
            padding: 16px 10px;
            flex-shrink: 0;
            display: flex;
            flex-direction: column;
            gap: 12px;
            scrollbar-width: thin;
        }

        .sidebar::-webkit-scrollbar {
            width: 4px;
        }
        .sidebar::-webkit-scrollbar-thumb {
            background: #cbd5e1;
            border-radius: 4px;
        }

        .sidebar-section-title, .group-title {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-muted);
            font-weight: 700;
            margin: 10px 0 4px 6px;
        }

        .tree-view ul, .tree-group {
            list-style-type: none;
            padding-left: 0;
            margin: 0;
            display: flex;
            flex-direction: column;
            gap: 2px;
        }

        .tree-view li, .tree-item {
            margin: 0;
        }

        .tree-view .file, .file {
            color: #475569;
            transition: all 0.15s ease;
            padding: 7px 10px;
            border-radius: 7px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 12.5px;
            cursor: pointer;
            user-select: none;
            white-space: nowrap;
            text-decoration: none;
        }

        .tree-view .file:hover, .file:hover {
            background-color: var(--primary-light);
            color: var(--primary-dark);
        }

        .tree-view .file.active, .file.active {
            background-color: var(--primary-color) !important;
            color: #ffffff !important;
            font-weight: 600;
            box-shadow: 0 2px 6px rgba(2, 132, 199, 0.28);
        }

        .file-title, .file > span:first-child {
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            flex: 1;
            margin-right: 8px;
            text-align: left;
            display: inline-block;
        }

        .badge, .pill {
            font-size: 10px;
            padding: 2px 7px;
            border-radius: 10px;
            background-color: #f1f5f9;
            color: #64748b;
            font-weight: 600;
            white-space: nowrap;
            flex-shrink: 0;
            display: inline-flex;
            align-items: center;
        }

        .pill-blue { background-color: #e0f2fe; color: #0369a1; }
        .pill-purple { background-color: #f3e8ff; color: #7e22ce; }
        .pill-green { background-color: #dcfce7; color: #15803d; }
        .pill-amber { background-color: #fef3c7; color: #b45309; }

        .tree-view .file.active .badge,
        .tree-view .file.active .pill,
        .file.active .badge,
        .file.active .pill {
            background-color: rgba(255, 255, 255, 0.95) !important;
            color: var(--primary-dark) !important;
            font-weight: 700;
        }

        /* 主内容区 */
        .main-content {
            flex: 1;
            padding: 24px 32px;
            overflow-y: auto;
            background-color: var(--bg-color);
            scroll-behavior: smooth;
        }

        .details-section {
            display: none;
            max-width: 1240px;
            margin: 0 auto;
            animation: fadeIn 0.18s ease-in-out;
        }

        .details-section.active {
            display: block;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(4px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .section-header, .section-title {
            font-size: 22px;
            font-weight: 800;
            color: #0f172a;
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 6px;
        }

        .section-header p, .section-desc {
            color: var(--text-muted);
            font-size: 13.5px;
            line-height: 1.6;
            margin-bottom: 20px;
        }

        /* 指标卡片网格 */
        .metric-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 16px;
            margin-bottom: 24px;
        }

        .metric-card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 16px 18px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
            display: flex;
            flex-direction: column;
            gap: 4px;
            border-left: 4px solid var(--primary-color);
        }

        .metric-card:nth-child(2) { border-left-color: var(--accent-color); }
        .metric-card:nth-child(3) { border-left-color: var(--secondary-color); }
        .metric-card:nth-child(4) { border-left-color: var(--warning-color); }

        .metric-label {
            font-size: 12px;
            color: var(--text-muted);
            font-weight: 600;
        }

        .metric-value, .metric-val {
            font-size: 23px;
            font-weight: 800;
            color: #0f172a;
            display: flex;
            align-items: baseline;
            gap: 8px;
            margin: 3px 0;
        }

        .metric-sub {
            font-size: 12.5px;
            font-weight: 700;
        }

        .metric-desc {
            font-size: 11.5px;
            color: #64748b;
            line-height: 1.4;
        }

        .text-green { color: var(--accent-color); }
        .text-blue { color: var(--primary-color); }
        .text-purple { color: var(--secondary-color); }
        .text-amber { color: var(--warning-color); }

        /* 提示框与说明条 */
        .notice-box, .callout {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-left: 4px solid var(--primary-color);
            border-radius: 0 8px 8px 0;
            padding: 14px 18px;
            margin-bottom: 22px;
            font-size: 13.5px;
            line-height: 1.65;
            color: #334155;
        }

        .notice-title {
            font-weight: 700;
            color: #0f172a;
            margin-bottom: 6px;
            font-size: 14px;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        /* 常用卡片 */
        .card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 20px;
            margin-bottom: 22px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.03);
        }

        .card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 16px;
            padding-bottom: 12px;
            border-bottom: 1px solid var(--border-color);
            flex-wrap: wrap;
            gap: 8px;
        }

        .card-title {
            font-size: 15px;
            font-weight: 700;
            color: #0f172a;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        /* Mermaid 渲染容器 */
        .mermaid-container, .mermaid-wrapper {
            overflow-x: auto;
            overflow-y: hidden;
            padding: 18px 8px;
            background: #ffffff;
            border-radius: 8px;
            display: flex;
            justify-content: center;
            min-height: 140px;
            width: 100%;
            -webkit-overflow-scrolling: touch;
        }

        .mermaid-container svg, .mermaid-wrapper svg, .mermaid svg {
            max-width: 100% !important;
            height: auto !important;
        }

        /* 表格与响应式 */
        .table-responsive {
            width: 100%;
            overflow-x: auto;
            margin-bottom: 16px;
            border-radius: 8px;
            border: 1px solid var(--border-color);
            -webkit-overflow-scrolling: touch;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            text-align: left;
        }

        th, td {
            padding: 10px 14px;
            border-bottom: 1px solid var(--border-color);
        }

        th {
            background: #f8fafc;
            font-weight: 600;
            color: #475569;
            white-space: nowrap;
        }

        tr:last-child td {
            border-bottom: none;
        }

        tr:hover td {
            background-color: #f8fafc;
        }

        /* 代码块与高亮 */
        .code-container {
            position: relative;
            margin: 12px 0 18px 0;
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid #1e293b;
        }

        .code-header {
            background: #1e293b;
            color: #94a3b8;
            padding: 8px 14px;
            font-size: 12px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-family: monospace;
        }

        .copy-btn {
            background: rgba(255, 255, 255, 0.1);
            color: #e2e8f0;
            border: 1px solid rgba(255, 255, 255, 0.15);
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 11px;
            cursor: pointer;
            transition: all 0.2s;
        }

        .copy-btn:hover {
            background: rgba(255, 255, 255, 0.2);
            color: white;
        }

        pre, code {
            font-family: 'JetBrains Mono', Consolas, Monaco, monospace;
        }

        pre {
            background: var(--code-bg);
            color: #f8fafc;
            padding: 14px 16px;
            overflow-x: auto;
            font-size: 12.5px;
            line-height: 1.6;
            margin: 0;
        }

        /* Live Trace 播放器与控制台 */
        .trace-controller {
            background: #ffffff;
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 16px 20px;
            margin-bottom: 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        }

        .trace-controls {
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 10px;
        }

        .trace-btn-group {
            display: flex;
            gap: 8px;
            align-items: center;
        }

        .trace-btn {
            padding: 6px 14px;
            border-radius: 6px;
            font-size: 12.5px;
            font-weight: 600;
            border: 1px solid var(--border-color);
            background: #ffffff;
            color: var(--text-color);
            cursor: pointer;
            transition: all 0.15s;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }

        .trace-btn:hover {
            background: #f1f5f9;
        }

        .trace-btn.primary {
            background: var(--primary-color);
            color: white;
            border-color: var(--primary-color);
        }

        .trace-btn.primary:hover {
            background: var(--primary-dark);
        }

        .trace-timeline-bar {
            width: 100%;
            height: 6px;
            background: #e2e8f0;
            border-radius: 3px;
            overflow: hidden;
            position: relative;
        }

        .trace-timeline-progress {
            height: 100%;
            background: var(--primary-color);
            width: 0%;
            transition: width 0.25s ease;
        }

        .trace-display-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }

        /* 移动端与平板响应式适配 */
        @media (max-width: 1200px) {
            .metric-grid {
                grid-template-columns: repeat(2, 1fr);
            }
        }

        @media (max-width: 960px) {
            header, .header {
                padding: 10px 16px;
                height: 56px;
            }

            .mobile-menu-btn {
                display: flex;
            }

            .brand-text p, .subtitle {
                display: none;
            }

            .brand-text h1, .title-row .title {
                font-size: 16px;
            }

            .header-badges, .header-right {
                display: none;
            }

            .sidebar {
                position: fixed;
                top: 0;
                left: 0;
                bottom: 0;
                width: 290px;
                max-width: 82vw;
                z-index: 100;
                transform: translateX(-100%);
                transition: transform 0.28s cubic-bezier(0.4, 0, 0.2, 1);
                box-shadow: 4px 0 20px rgba(0,0,0,0.18);
                padding-top: 14px;
            }

            .sidebar.open {
                transform: translateX(0);
            }

            .sidebar-backdrop.active {
                display: block;
            }

            .main-content {
                padding: 16px 14px;
            }

            .metric-grid {
                grid-template-columns: repeat(2, 1fr);
                gap: 10px;
            }

            .trace-display-grid {
                grid-template-columns: 1fr !important;
            }

            .card {
                padding: 14px;
            }

            .section-header h2, .section-title {
                font-size: 18px;
            }
        }

        @media (max-width: 640px) {
            .metric-grid {
                grid-template-columns: 1fr;
            }

            .brand-icon, .logo {
                width: 32px;
                height: 32px;
                font-size: 17px;
            }

            .header-tag, .title-row .badge {
                display: none;
            }

            .metric-value, .metric-val {
                font-size: 20px;
            }

            .main-content {
                padding: 12px 10px;
            }

            .card {
                padding: 12px;
            }
        }
"""
