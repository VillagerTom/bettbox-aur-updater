# bettbox-aur-updater

 [**Bettbox**](https://github.com/appshubcc/Bettbox) 相关 AUR 包的管理仓库，使用 CI 自动维护版本更新。

AUR 包以 git 子模块形式托管在 `aur/*/` 目录下，当前包含：

| AUR 包 | 架构 | 类型 | 子模块路径 |
|--------|------|------|-----------|
| [bettbox-compatible](https://aur.archlinux.org/packages/bettbox-compatible) | x86_64 | 源码构建 | `aur/bettbox-compatible/` |
| [bettbox-compatible-bin](https://aur.archlinux.org/packages/bettbox-compatible-bin) | x86_64 | 预编译二进制 | `aur/bettbox-compatible-bin/` |

## 工作流程

### [`update-aur.yaml`](.github/workflows/update-aur.yaml)

定时（每天 4:30 / 16:30 UTC）或手动触发：

1. **Check** — 查询 [appshubcc/Bettbox](https://github.com/appshubcc/Bettbox) 最新 release tag，对比所有子模块当前版本
2. **Update** — 遍历 `aur/*/`，从上游获取对应的 SHA256，更新 PKGBUILD / .SRCINFO，push 到 aur.archlinux.org
3. **Parent pointer** — 更新父仓库的子模块指针

### [`sync-from-aur.yaml`](.github/workflows/sync-from-aur.yaml)

手动触发，将 AUR 上的最新提交同步回父仓库指针（用于 AUR 被独立修改后的逆向同步）。在 commit body 中记录每个子模块的新增 commit log。

## 子模块管理

```bash
# 添加新 AUR 包
git submodule add ssh://aur@aur.archlinux.org/<pkgname>.git aur/<pkgname>
```

对应 CI 会自动识别并开始管理。

## 许可证

本项目 [GPL-3.0-or-later](LICENSE)

[上游](https://github.com/appshubcc/Bettbox) 使用 [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html)
