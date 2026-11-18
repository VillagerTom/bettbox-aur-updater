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

更新管道是 [`update-aur.yaml`](.github/workflows/update-aur.yaml)：

| 触发 | 场合 |
|------|------|
| `schedule`（每天 4:30 / 16:30 UTC） | 例行检查上游发版 |
| `workflow_dispatch` | 手动运行，可传 `force` / `dry_run` |
| `repository_dispatch`（`bettbox-release`） | [Bettbox](https://github.com/appshubcc/Bettbox) 发版流程 |
| `workflow_call` | 由 [`update-aur-approval.yaml`](.github/workflows/update-aur-approval.yaml) 在批准 mod request 时调用 |

[`update-aur-approval.yaml`](.github/workflows/update-aur-approval.yaml) 是 issue 评论的唯一入口：批准评论先过一次内联预检（读请求与子模块里的已发布版本，判「现在落地会不会有变化」），只有会落地才调用同样的管道；每次运行还会清掉之前的空跑，只留下真正动过包的记录。

一次运行做完解析、落地与父指针更新。`force` 绕过版本判定、版本不变也提 `pkgrel`（用于刷新 checksum），`dry_run` 只预览。

修改打包内容走 **mod request**（issue，按版本号锚定）：`pending` 拦下本来要推进的版本，`ready` 随该版本一起落地，版本不动时也能单独落地。格式与操作见 [skill](.agents/skills/mod-request/SKILL.md)。

[`sync-from-aur.yaml`](.github/workflows/sync-from-aur.yaml) 是反向同步：手动触发，把 AUR 上的最新提交同步回父仓库指针。

版本判定、逐包落地规则、提交信息与容器限制等细节见 [AGENTS.md](AGENTS.md)。

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