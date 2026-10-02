# bettbox-aur-updater

[**Bettbox**](https://github.com/appshubcc/Bettbox) 相关 AUR 包的管理仓库，使用 CI 自动维护版本更新。

AUR 包以 git 子模块形式托管在 `aur/*/` 目录下，分 **stable / pre 双通道**，共 7 个包：

| AUR 包 | 通道 | 架构 | 类型 | 子模块路径 |
|--------|------|------|------|-----------|
| [bettbox](https://aur.archlinux.org/packages/bettbox) | stable | x86_64 / aarch64 | 源码构建 | `aur/bettbox/` |
| [bettbox-compatible](https://aur.archlinux.org/packages/bettbox-compatible) | stable | x86_64 | 源码构建（`GOAMD64=v1`） | `aur/bettbox-compatible/` |
| [bettbox-compatible-bin](https://aur.archlinux.org/packages/bettbox-compatible-bin) | stable | x86_64 | 预编译二进制 | `aur/bettbox-compatible-bin/` |
| [bettbox-pre](https://aur.archlinux.org/packages/bettbox-pre) | pre | x86_64 / aarch64 | 源码构建 | `aur/bettbox-pre/` |
| [bettbox-compatible-pre](https://aur.archlinux.org/packages/bettbox-compatible-pre) | pre | x86_64 | 源码构建（`GOAMD64=v1`） | `aur/bettbox-compatible-pre/` |
| [bettbox-compatible-pre-bin](https://aur.archlinux.org/packages/bettbox-compatible-pre-bin) | pre | x86_64 | 预编译二进制 | `aur/bettbox-compatible-pre-bin/` |
| [bettbox-pre-bin](https://aur.archlinux.org/packages/bettbox-pre-bin) | pre | x86_64 / aarch64 | 预编译二进制 | `aur/bettbox-pre-bin/` |

- **pre 通道是 stable 的超集**：两个通道各自独立解析上游——stable 只认正式 tag，pre 同时认正式 tag 和 `-pre` tag 并取版本号最大者。
- **同通道内原子更新**：一个通道内的包永远同一版本；两通道互不干扰。stable 通道 3 包，pre 通道 4 包。
- 7 包安装结构一致（`usr/lib/bettbox`、`usr/bin/bettbox` 软链、`provides=bettbox=$pkgver`），且互相 `conflicts`，同一时间只能安装其一。
- `compatible` 后缀表示用 `GOAMD64=v1` 构建（仅 x86_64）；`compatible-bin` 同时只提供 x86_64。`bettbox-pre-bin` 上游发布了 amd64 与 arm64 两个 deb，因此支持双架构。
- `bettbox-bin` 是另一维护者（lyj404）的包，本仓库的 7 个包均与其 `conflicts`。

## 工作流程

本节只列流程概览与手动输入。版本判定规则、提交信息规范、容器环境限制等操作细节见 [AGENTS.md](AGENTS.md)。

### [`update-aur.yaml`](.github/workflows/update-aur.yaml)

定时（每天 4:30 / 16:30 UTC）或手动触发。job 运行在 `archlinux:base-devel` 容器内，使用 Arch 原生工具链：

1. **Check** — 每包按自己的 `.nvchecker.toml`（GitHub releases API）解析通道目标版本
2. **Update** — 遍历 `aur/*/`：更新 pkgver/pkgrel，`updpkgsums` 刷新 checksum，`makepkg --printsrcinfo` 重生成 `.SRCINFO`，push 到 aur.archlinux.org
3. **Parent pointer** — 更新父仓库的子模块指针

写入 AUR 前有两重短路（作业级 + 逐包级）：版本已等于目标的包会被整包跳过，`pkgrel` 只在版本真正变化时归 1。

手动触发可传两个 input：

- `force`：绕过两重短路，即使版本已等于通道目标也照样提 `pkgrel`（+1），用于刷新 checksum / 强制重推
- `dry_run`：只预览 PKGBUILD/.SRCINFO 的 diff，不提交不推送（与 `force` 组合 = 预览 pkgrel+1 的产物）

### [`sync-from-aur.yaml`](.github/workflows/sync-from-aur.yaml)

手动触发，将 AUR 上的最新提交同步回父仓库指针（用于 AUR 被独立修改后的逆向同步）。在 commit body 中记录每个子模块的新增 commit log。

## 子模块管理

```bash
# 首次克隆
git clone --recurse-submodules <url>

# 添加新 AUR 包
git submodule add ssh://aur@aur.archlinux.org/<pkgname>.git aur/<pkgname>
```

对应 CI 会自动识别并开始管理。

## 许可证

本项目 [GPL-3.0-or-later](LICENSE)

[上游](https://github.com/appshubcc/Bettbox) 使用 [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html)