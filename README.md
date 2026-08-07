# Peak 代练 - 可提交订单的站点

这是一个可真实提交订单的游戏代练落地页：表单会 POST 到订单服务，订单持久化保存在
SQLite（`orders.db`），页面底部「我的订单」可按联系方式实时查询；管理后台
（`/admin`）可查看全部订单、修改状态、删除测试数据。

## 运行

需要 Python 3（纯标准库，无需安装任何依赖）：

```powershell
python order_server.py
```

然后浏览器打开（管理后台在 `/admin`，默认密码 `admin123`）：

```
http://localhost:8000
http://localhost:8000/admin
```

默认端口 8000，可指定其他端口：

```powershell
python order_server.py --port 8080
```

首次启动会自动把旧版 `orders.json` 中的数据迁移进 `orders.db`（仅当数据库为空时）。
生产环境请务必设置环境变量 `ADMIN_PASSWORD`，否则使用默认密码 `admin123`。

> 注意：直接用浏览器双击打开 `game-boosting.html` 也能看页面，但提交订单和查询订单
> 必须通过上面的本地服务器访问，否则无法连接 `/api/orders`。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/orders` | 提交订单，JSON body：`game/service/currentRank/targetRank/contact` |
| GET | `/api/orders` | 返回全部订单 |
| GET | `/api/orders?contact=微信或QQ` | 按联系方式筛选订单 |
| POST | `/api/admin/login` | 管理员登录（body：`password`），返回 session cookie |
| GET | `/api/admin/me` | 检查管理员登录态 |
| GET | `/api/admin/orders` | 全部订单（需登录） |
| POST | `/api/admin/orders/<id>/status` | 修改状态（需登录） |
| DELETE | `/api/admin/orders/<id>` | 删除订单（需登录） |

订单保存位置：`order_server.py` 同目录下的 `orders.db`（SQLite）。

## 部署到公网

项目已包含部署配置，支持 Render / Railway 等 PaaS：

### Render（免费额度）

1. 把本目录推到 GitHub 仓库（`game-boosting.html`、`admin.html`、`order_server.py`、
   `requirements.txt`、`render.yaml`）。
2. 在 [render.com](https://render.com) 新建 Blueprint（关联 GitHub 仓库），
   Render 会自动读取 `render.yaml`。
3. 首次部署时填写 `ADMIN_PASSWORD` 环境变量（Blueprints 里会提示你设置）。
4. 部署完成后访问 Render 提供的 `https://xxx.onrender.com`。

### Railway / VPS

- Railway：新建服务选择本仓库，启动命令读取 `Procfile` 即可，记得设置
  `ADMIN_PASSWORD` 和 `PORT`。
- VPS：`python order_server.py`（设置 `HOST=0.0.0.0`、`PORT=80`），
  前面再挂 Nginx/Caddy 反代即可。

> 免费 PaaS 的磁盘是临时的，重启后 SQLite 数据会丢失。若需要长期保留订单，
> 建议后续接入 Postgres（如 Supabase / Neon 免费库），或把 `DB_PATH` 指向持久卷。

## 文件

- `game-boosting.html` - 页面（含全屏游戏背景、订单表单、我的订单查询）
- `admin.html` - 管理后台（登录后可查看/改状态/删除订单）
- `order_server.py` - 本地订单服务
- `orders.db` - SQLite 订单数据库（首次启动自动创建，自动迁移旧 orders.json）
- `render.yaml` / `Procfile` / `requirements.txt` - 部署配置
