import os
import sys
from pathlib import Path

from backend.env import PROJECT_ROOT, load_env

# 必须先加载环境变量，再导入后续会在模块顶层读取 os.getenv 的业务模块。
load_env()


from backend.api.router import router
from backend.infra.database import init_db
# 兼容 `python backend/app.py`：脚本直跑时先把仓库根目录加入模块搜索路径。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# 开发期通常由 Vite 提供页面；只有生产构建存在时才由 FastAPI 托管。
FRONTEND_DIR = PROJECT_ROOT / "frontend" / "dist"


def create_app() -> FastAPI:
    app = FastAPI(title="企业知识库智能问答系统 API")

    # 这是便于本地联调的宽松配置；生产环境必须限制允许的来源。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def startup_init_db():
        # 开发期启动时创建缺失表；它不会迁移已存在表的字段结构。
        init_db()

    # 先注册 API，再在入口末尾挂载 `/` 静态目录，避免静态路由吞掉接口。
    app.include_router(router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # 不存在 dist 时跳过挂载，让纯后端开发和测试仍可正常启动。
    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    # 环境变量只影响脚本直跑；uvicorn CLI 会使用命令行参数。
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8050")))
