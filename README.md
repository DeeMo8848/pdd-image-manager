# 本体图片管理工具

喜欢收集各种图片的屯屯鼠用ai跑的本地图片管理与整理工具。

## 功能

- **相册浏览** — 按文件夹浏览已整理图片，支持嵌套相册、标签筛选、排序
- **图库整理** — 一键将散图移动至对应相册，支持撤回、垃圾桶
- **标签管理** — 为相册添加多个标签，按标签筛选
- **垃圾桶** — 删除的图片/文件夹可恢复或彻底删除，支持清空
- **图片查看器** — 更新了缩放、拖拽平移、删除/移动到其他相册
- **长按滑动多选** — 更新了鼠标滑动批量选择
- **预览图缓存** — 更新了自动生成缩略图，加速浏览

## 技术栈

- 后端：Python 3 + FastAPI + SQLite + Pillow
- 前端：原生 HTML/CSS/JS，无框架

## 快速开始

```bash
pip install fastapi uvicorn python-multipart pillow
python main.py
```

或直接双击 `start.bat`，浏览器自动打开 `http://localhost:8901`。
首次使用需要在设置中添加路径

## 目录结构

```
├── main.py            # FastAPI 后端
├── templates/
│   └── index.html     # 前端界面
├── requirements.txt
├── start.bat          # Windows 一键启动
├── settings.json      # 配置（目录路径等）
├── manager.db         # SQLite 数据库（标签）
├── thumb_cache/       # 预览图缓存
└── static/            # 静态资源
```
