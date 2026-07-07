# LdxpM

LdxpM 是一个 `pay.ldxp.cn` 店铺库存监控服务。它在后端持续监控多个店铺的商品库存和价格变化，网页只作为控制台使用。

## 功能

- 多店铺监控，支持添加/删除店铺。
- 商品列表展示库存、价格、价格变化和原商品链接。
- 当前上架/未上架商品分开展示，店铺变空时会保留历史商品但不再混入默认列表。
- 商品可切换“重点监控 / 非重点监控”。
- 补货、价格变化、库存减少、售罄、商品未上架提醒。
- 重点商品提醒会带 `【重点通知】`。
- 库存减少提醒会按商品聚合：累计减少满 5 个后进入提醒队列，每 30 秒合并发送一次。
- 售罄提醒即时发送。
- SMTP 邮件通知。
- 后端独立运行，不依赖网页打开。
- Docker / Docker Compose 部署，服务器直接拉取预构建镜像，不需要本地编译。

## 快速安装

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/iKeilo/LdxpM/main/install.sh)
```

如果你的系统默认分支不是 `main`，可以把命令里的 `main` 替换成实际分支名。

## 管理命令

安装脚本是交互式菜单，支持安装、更新、卸载、查看状态：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/iKeilo/LdxpM/main/install.sh)
```

也可以直接执行指定操作：

```bash
# 安装
bash <(curl -fsSL https://raw.githubusercontent.com/iKeilo/LdxpM/main/install.sh) install

# 更新，从 GitHub 拉取最新项目并拉取预构建镜像
bash <(curl -fsSL https://raw.githubusercontent.com/iKeilo/LdxpM/main/install.sh) update

# 卸载
bash <(curl -fsSL https://raw.githubusercontent.com/iKeilo/LdxpM/main/install.sh) uninstall

# 状态
bash <(curl -fsSL https://raw.githubusercontent.com/iKeilo/LdxpM/main/install.sh) status
```

默认安装目录：

```text
/opt/ldxpm
```

默认端口：

```text
8765
```

安装完成后脚本会显示：

- 本机地址：`http://127.0.0.1:端口`
- 局域网地址：`http://服务器IP:端口`

## 手动 Docker 部署

```bash
git clone https://github.com/iKeilo/LdxpM.git
cd LdxpM
mkdir -p data
PORT=8765 docker compose pull
PORT=8765 docker compose up -d
```

访问：

```text
http://服务器IP:8765
```

## 数据持久化

SQLite 数据库保存在容器外：

```text
./data/ldxp_stock_webapp.sqlite3
```

请不要把数据库提交到 GitHub，因为里面可能包含 SMTP 配置。

## 邮件配置

进入网页后，在“邮件通知”区域填写：

- SMTP 服务器
- 端口
- 加密方式：`STARTTLS / 587`、`SSL / 465` 或 `不加密`
- SMTP 用户名
- SMTP 密码或授权码
- 发件人
- 收件人

常见配置：

- 端口 `465`：选择 `SSL / 465`
- 端口 `587`：选择 `STARTTLS / 587`

## 更新

使用安装脚本的“更新”菜单，或执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/iKeilo/LdxpM/main/install.sh) update
```

如果你是手动部署：

```bash
cd /opt/ldxpm
git pull --ff-only
docker compose pull
docker compose up -d
```

## Releases 和 Packages

项目使用 GitHub Actions 自动生成预构建产物：

- GitHub Packages / GHCR Docker 镜像：`ghcr.io/ikeilo/ldxpm`
- Docker 镜像架构：`linux/amd64`、`linux/arm64`、`linux/arm/v7`
- GitHub Releases 二进制包：Linux x86_64、Linux ARM64、macOS x86_64、macOS ARM64

推送 `main` 分支会自动发布 `latest` Docker 镜像。推送版本标签会同时生成 Docker 版本镜像和 Release 附件：

```bash
git tag v1.0.0
git push origin v1.0.0
```

安装脚本默认使用 `IMAGE_TAG=latest`。如果要固定版本，可以在安装或更新前指定：

```bash
IMAGE_TAG=1.0.0 bash <(curl -fsSL https://raw.githubusercontent.com/iKeilo/LdxpM/main/install.sh) install
```

二进制包可直接运行，默认监听 `127.0.0.1:8765`。如需指定监听地址、端口和数据库路径：

```bash
HOST=0.0.0.0 PORT=8765 DB_PATH=./ldxp_stock_webapp.sqlite3 ./ldxpm
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | 服务监听地址 |
| `PORT` | `8765` | Web 端口 |
| `DB_PATH` | `/app/data/ldxp_stock_webapp.sqlite3` | SQLite 数据库路径 |
| `BASE_URL` | `https://pay.ldxp.cn` | 目标站点地址，测试时可指向假上游 |
| `DEFAULT_SHOP_URL` | `https://pay.ldxp.cn/shop/WPXSCE1B/` | 首次启动自动添加的默认店铺 |

## 测试

项目包含一个黑盒回归测试，会启动假购物站和本服务，模拟“店铺从有商品变成商品未上架”的场景：

```bash
python tests/blackbox_empty_shop.py
```

## 安全提醒

- 请不要公开暴露到公网，除非你在前面加了认证、反向代理或防火墙。
- SMTP 密码会保存在 SQLite 数据库中，请保护好 `data/` 目录。
