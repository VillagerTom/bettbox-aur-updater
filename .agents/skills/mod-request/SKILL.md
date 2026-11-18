---
name: mod-request
description: 生成、校验并发布 mod-request issue（AUR 打包改动请求），或解析/审计已有 issue。用于上游 breaking change 适配、多人协作提案，以及按版本号锚定、pre 与正式版按各自节奏应用的打包修改。
---

# mod-request

一个 **mod request**（下文 M-R）是对 `aur/*` 打包内容的改动请求。它同时承担三件事：

1. **版本门禁**——存在一条覆盖某包、且 `target_version` ≤ 该包目标版本的 open M-R 时，`update-aur` 不得把该包推进到那个版本（与 `diff_state` 无关）。
2. **内容载体**——`diff_state: ready` 时携带每包的 diff 与提交信息，供 CI 应用。
3. **进度记录**——CI 每次应用更改就重写那条带签名的 **applied record** 评论：文首是一行隐藏的
   HTML 注释，装着记录内容与它的 SSH 签名；紧跟一行可见的进度说明，下面是整个包选集的勾选列表
   （已应用的逐行标注应用后的完整版本）；它覆盖的包全部应用后关闭 issue。注释、摘要行、勾选列表
   都属于这条记录，但只有签名能证明这条记录出自 CI——摘要与勾选是签名里 `applied` 的渲染。

## 硬规则

- **单一实现**：issue 正文格式只由 `scripts/mod-request.py` 定义。永远不要手写或凭记忆拼格式；渲染、解析、哈希、校验都走它。
- **锚点是 base semver**：`target_version` 形如 `1.20.0`，不带 `-pre` / `-test` 后缀。`1.20.0pre1` 与 `1.20.0` 同属一个版本家族，门禁按 `base(包目标版本) >= target_version` 触发——这正是 pre 先应用、stable 后跟上的原因。
- **无 backport**：AUR 只保留 `master` 一条分支，这是它的固有性质、不是本仓库的策略——唯一存在的 tip 就是当前版本，改动无处回溯。`target_version` 不得早于任一受影响包的当前目标版本；那等于声明一次无法表达的回溯。
- **diff 不得触碰 `pkgver=` / `pkgrel=` / `.SRCINFO`**：这三样归 CI 独占。`check` 会拒绝。
- **同一版本再改动：看 CI 有没有在这个 issue 上评论带签名的 applied record。** 没有（还没有任何包
  被应用）→ **原地改正文**。有（哪怕只应用了一个包）→ **另开新 issue**，并在新 issue 的 `supersedes`
  里填旧 issue 编号。判据是**签名**：勾选与摘要是这条记录的组成部分（签名里 `applied` 的渲染），
  手改它们不会让记录变成「已应用」，也骗不过门禁。
- **一个包集同时只有一条 live 请求**：diff 只对一个树状态定义，格式没有「基于另一条请求」的表达——
  两个 open + `approved` 的请求若解析出同一个包（无论版本、无论 `pending` 还是 `ready`），后批准的会
  被 gate 拒绝，否则第二个的基树是歧义的，落地时要么 hunk 对不上、要么落上一个没人构建过的组合。前一条
  落地并关闭（或在新请求的 `supersedes` 里声明）之后才能批准下一条。**版本不在判据里**：同版本但包集
  不相交的请求互不影响（`bin` 修与 `source` 修可以并行，同一轮各自落地）。

## 前置

工具：`python3`、`git`、`jq`、`yq`（仅 `check-form`）、`gh`、`ssh-keygen`（验签只需公钥）。

## 数据模型

| 字段 | 类型 | 说明 |
|---|---|---|
| `supersedes` | issue 编号 | 仅当本请求取代一个同版本、已有 applied record 的旧请求时填，如 `#12`；否则留空 |
| `summary` | 单行 | 改动目的 |
| `target_version` | base semver | 这次改动适配的版本；门禁阈值 |
| `next_update` | 复选框 | 勾上 = 不应用到当前已发布的版本，等该包下一次升版本时再启用 |
| `type` | 多选 `source` / `bin` | 源码构建 / 预编译二进制 |
| `variant` | 多选 `common` / `compatible` | 常规构建 / `GOAMD64=v1` 兼容构建 |
| `edition` | 多选 `standard` / `lite` | 常规版 / 轻量版（`lite` 上游已发布，本仓库尚未实装，选中会校验失败） |
| `diff_state` | `pending` / `ready` | `pending` 只参与门禁，不携带 diff |
| `evidence` | 自由文本 | 本地验证证据，CI 读不到，只记录 |
| `payload: <pkg>` | 每包一段 | 该包的 diff + `message`；仅 `ready` 时非空 |

**包不直接列出来**，由三个维度按 `aur/*` 里实际存在的包解析：`bettbox[-lite][-compatible][-pre][-bin]`。
通道不在维度里——一选就是 stable 与 pre 两个孪生包，各自到达 `target_version` 时生效。
所以选 `source` + `common` 会解析出 `bettbox` 与 `bettbox-pre` 两个包；选 `bin` + `common` 只解析出
`bettbox-pre-bin`（stable 的 `bettbox-bin` 是另一位维护者的包，不在本仓库）。

`supersedes` 是正文里唯一能声明的**关系**：一个版本已经部分应用、需要再改时，不是编辑旧 issue，而是
另开一条并把旧编号写在这里。它只是声明，不是权限——取代关系由 `supersede` 校验（同
`target_version`、新请求包含旧请求覆盖的全部包），通不过就是无效声明。CI 依据它决定关哪条、不应用
哪条，所以填错比留空更糟：留空等于「两条并存的请求」，会被当成冲突。

`next_update` 管的是 `target_version` 表达不了的那件事：当 `target_version` 正好等于某个包已所在版本的
base semver（pre 通道常见——`1.20.0pre1` / `pre2` / `pre3` 是同一个 base）时，勾上它表示「别改已经发出去
的那个版本，等下一个版本」。CI 的判定是：这条请求只在该包版本发生变动的那次运行里生效。

**什么时候生效**由 `diff_state` 与 `next_update` 一起决定，逐包判定：`ready` 且未勾 `next_update` 时，
只要某包已发布到 `target_version`（base 相等即算，`1.20.0pre3` 算到了 `1.20.0`），批准就立刻落地
（作为 `pkgrel` 补丁）——审批入口先做一次内联预检，证明「现在落地不了」（`pending`、勾了
`next_update`、版本还没到）的批准不会起管道，等该包升版本的那次运行或下一次 cron。CI 不保存计划，
每次运行都从 issue 重新判定。

正文里**每个包一段** `### payload: <pkgname>`，段内两个四反引号代码块（先 `diff` 后 `text`）。
没被解析出来的包、以及 `pending` 状态下的所有包，该段留空即可。

**哈希只覆盖会影响结果的部分**（`BOUND_FIELDS`）：三个维度、`target_version`、`next_update`、
`diff_state`、`supersedes`，以及每包的 diff 与 `message`。`summary` 与 `evidence` 不进哈希——
它们是给人看的，改错字不需要重新 approve。反过来说，任何改变 CI 动作、或改变 AUR 收到内容的修改
都要重新 approve，包括 diff 里改一行、`message` 改一句。

## 流程 A：改动尚未确定（先占位）

适用：上游已发 breaking，适配还没写出来。目的是**立刻挡住版本推进**，避免 CI 把未适配的包推上通道。

```sh
# 1. 写数据文件（payload 留空）
cat > /tmp/mr.json <<'JSON'
{
  "summary": "上游 PR #159 把构建逻辑并入 setup.dart",
  "target_version": "1.20.0",
  "type": ["source"],
  "variant": ["common", "compatible"],
  "edition": ["standard"],
  "diff_state": "pending",
  "evidence": "上游 PR 已合并，尚未本地验证"
}
JSON

# 2. 渲染 + 校验 + 发布（脚本位于仓库根 scripts/ 下）
S=scripts/mod-request.py
python3 "$S" render -d /tmp/mr.json > /tmp/mr.md
python3 "$S" check -f /tmp/mr.md
gh issue create --title "[mod] setup.dart --build-only" --body-file /tmp/mr.md
```

> 不需要标签：请求靠正文格式识别，`approved` 由维护者审批时加上。所以第三方也能走同一条命令，
> 或直接用 [Issue Form](../../../.github/ISSUE_TEMPLATE/mod-request.yml)；两条路径产出的正文
> 是同一格式，最终都在同一个 gate 汇合。想让浏览 issue 列表的人一眼认出，标题保持 `[mod] ` 前缀。

想确认这次选择会选中哪些包，看 `python3 "$S" resolve -f /tmp/mr.md`。

## 流程 B：改动已确定（产出每包 diff）

```sh
# 1. 在子模块里改包（子模块常处于 detached HEAD，改动不提交即可）
#    改完取整个包目录的 diff（不要 -- 限定单个文件，附属文件也算改动）：
git -C aur/bettbox-pre diff > /tmp/bettbox-pre.diff
git -C aur/bettbox    diff > /tmp/bettbox.diff

# 2. 若不想把工作区改动留在子模块里（会丢弃未提交改动，确认后执行）：
# git -C aur/bettbox-pre restore .
```

一项选择同时覆盖 stable 与 pre 一对孪生包，所以两个包都要有自己的 diff——它们的 `PKGBUILD`
本来就不是同一份文件（`pkgname`、`_pkgver`、`conflicts` 等都不同）。

把每个包的 diff 与 `message` 写进数据文件。按 Conventional Commits 编写 `message`（首行
`<type>(<pkg>): <what changed>`，正文说明原因），CI 再按包决定它的用法：

- 该包本次不升版本（已经在 `target_version`）→ 直接作为那条 AUR 提交的提交信息；
- 该包本次正要升到 `target_version` → 作为 `Update to v<ver>` 提交的正文。

两种用法都读得通即可——同一条请求的两个通道可能分别落在两种情形（pre 先到、stable 后到），所以
issue 格式上不把它拆成 title / body。

```json
{
  "supersedes": "#12",
  "summary": "适配上游 setup.dart --build-only",
  "target_version": "1.20.0",
  "type": ["source"],
  "variant": ["common"],
  "edition": ["standard"],
  "diff_state": "ready",
  "evidence": "~/Projects/archpkg/bettbox-compatible-test-bin @ 1.19.4test677-4",
  "payload": {
    "bettbox": {
      "diff": "--- a/PKGBUILD\n+++ b/PKGBUILD\n@@ ...",
      "message": "fix(pkg): use setup.dart --build-only\n\nUpstream PR #159 merged the core build into setup.dart."
    },
    "bettbox-pre": {
      "diff": "--- a/PKGBUILD\n+++ b/PKGBUILD\n@@ ...",
      "message": "fix(pkg): use setup.dart --build-only\n\nUpstream PR #159 merged the core build into setup.dart."
    }
  }
}
```

> 手写 JSON 里的 `\n` 容易出错。可先用 `jq -Rs` 把 diff 文件包成字符串，再 `jq` 组装数据文件。

```sh
S=scripts/mod-request.py
python3 "$S" render -d /tmp/mr.json > /tmp/mr.md
python3 "$S" check -f /tmp/mr.md          # 必过；失败不要发
gh issue create --title "[mod] <summary>" --body-file /tmp/mr.md
```

## 本地预演：把请求落到工作区

`scripts/apply-mod-request.sh` 既是 CI 的判定与落地实现，也是本地落地入口——同一份版本规则，没有第二处：

```sh
# 读 issue，应用到它覆盖的所有包；工作区保持 dirty，便于随后 build
scripts/apply-mod-request.sh 12

# 只预演一个包，什么都不改
scripts/apply-mod-request.sh 12 --pkg bettbox --dry-run

# 不读 issue，直接用正文文件；--versions 给出「本轮把包推进到哪个版本」
scripts/apply-mod-request.sh -f /tmp/mr.md --versions /tmp/targets.txt
```

不带 `-f` 也不带 issue 号时脚本会自己拉 live 请求（CI 的用法）；本地预演请显式给 issue 号或正文。

`/tmp/targets.txt` 每行 `<包> <当前> <目标>`。没有 `--versions` 时按 PKGBUILD 里的 `pkgver` 视为不动。

版本规则：包必须已到 `target_version`（或本次正被推进到它）、不得比它更新（无 backport）、勾了
`next_update` 的请求不会重复落到已发布的版本上；不满足就跳过并说明原因。

`--force` 忽略这条规则，**只给本地/手动用**——CI 以 live 模式调用（无 `-f`/issue）时不传它，live 模式也会直接拒绝它。
落地后按脚本打印的提示跑 `updpkgsums` 与 `makepkg`，回滚命令也在输出里。

## 同一版本再改动

判据只有一条：**CI 有没有在这个 issue 上评论带签名的 applied record**。每次应用更改，CI 重写这条
评论一次：文首是一行隐藏注释，写着 issue、目标 base 版本、已应用的包与各自时间和各自应用后的
完整版本，并附一段覆盖这些内容的 SSH 签名；下面一行是可见的进度说明，再往下是整个包选集的勾选
列表——它是签名里 `applied` 的渲染。
还没应用的包只在这里出现，且不带版本——它还没发布过任何东西：

```markdown
<!-- mr-applied: {"applied":{"bettbox-pre":{"at":"2026-11-16T10:45:22Z","full":"1.20.0pre1-2"}},"issue":12,"kind":"applied","sig":"U1NIU0lH…","version":"1.20.0"} -->
**Applied at v1.20.0** — 1 of 2 packages

- [ ] `bettbox`
- [x] `bettbox-pre` — 1.20.0pre1-2
```

摘要行已经写明目标 base 版本，所以每行只在应用后标注**落地时的完整版本**（`pkgver-pkgrel`）；pre 与
stable 到同一个 base 时是两个不同的构建，这里显示的是各自实际发出的那个。

要确认这条记录是不是伪造：

```sh
S=scripts/mod-request.py
n=12
gh issue view "$n" --json body -q .body > /tmp/body.md
gh issue view "$n" --json comments -q '.comments' > /tmp/comments.json
python3 "$S" applied --issue "$n" --body /tmp/body.md --comments /tmp/comments.json
# 逐行打印验签通过、版本与 issue 都对得上的包
```

| 状态 | 做法 |
|---|---|
| 没有 applied record | **原地改正文**。改完在 issue 里评论一句：请求已修改，请重新审批（正文一改，原来的批准记录就失效了） |
| 已有 applied record（M-R 已被至少一个包应用） | **另开新 issue** 取代它 |

「已有 applied record」也是取代能成立的前提：旧 issue 上没有这条记录时，它还没有任何包被应用，
`supersedes` 指向它会被拒——那时应当原地改正文。

改包集、撤包、改 diff 都是正文改动，走同一条判据，没有例外路径。另开新 issue 时：

1. 新 issue 的 `supersedes` 填旧 issue 编号（数据文件里写 `"supersedes": "#12"`，正文里也是 `#12`）；
2. 旧 issue 在这个取代成立时关闭，留下 `Closed in favour of #<new>`；
3. 新 issue 的 tasklist 从零开始。

取代关系在发布前就能自查，用的是同一个判定：

```sh
S=scripts/mod-request.py
python3 "$S" supersede --new /tmp/new.md --old /tmp/old.md
# 0  可以取代（同 target_version，新请求包含旧请求的全部包）
# 2  新请求没有包含旧请求覆盖的包（关闭旧 issue 就会丢包）
# 1  不是取代关系（target_version 不同，或旧正文已解析不了）
```

写进正文的规范形式是 `#12`（`12`、`  #12  ` 只要在 `check` 里出现就会被归一化成 `#12`，所以哈希与
打字方式无关）；`#12a`、`#0`、数据文件里的裸 `12` 会被 `check` 拒。

## 解析已有 issue（审计）

```sh
S=scripts/mod-request.py
gh issue view <n> --json body -q .body > /tmp/body.md
python3 "$S" check -f /tmp/body.md      # 结构/规则校验；通过时打印 sha256
python3 "$S" parse -f /tmp/body.md      # canonical JSON
python3 "$S" resolve -f /tmp/body.md    # 这条请求实际覆盖哪些包，一行一个
python3 "$S" target -f /tmp/body.md     # 只打印 target_version（旧正文缺字段也能读）
```

`check` 通过只说明正文**结构**合法，它不判断 `supersedes` 指向的是不是真关系（那需要 issue 数据，
脚本离线）。判断只有一处实现：

```sh
python3 "$S" gate --issue <n> --body /tmp/body.md \
  --open-approved /tmp/live.json [--old /tmp/old.json]
# {"ok": true, "close": 12, "reasons": [], ...}   退出码 0
# {"ok": false, "close": null, "reasons": [...]}  退出码 1
```

`--open-approved` 是「已经占用了某个版本的请求」名单（`[{number, body}]`），`--old` 是被 `supersedes`
点名的那个 issue（`{number, body, state, comments}`）。审批与应用都调用它，所以同一份正文不会出现
「批的时候合法、用的时候不合法」；CI 侧用 `--no-close`（关闭只发生在批准取代时）。

## 范围限制：能改哪些文件

payload 里装的是 `git -C aur/<pkg> diff` 的原样输出，路径以**该包目录为根**，可以改目录下的任意文件
（`PKGBUILD`、`.install`、`.hook`、`.desktop`、`LICENSE`、补丁等）。禁止三类：

| 禁止 | 原因 |
|---|---|
| `.SRCINFO` | 由 CI 的 `makepkg --printsrcinfo` 生成 |
| `.nvchecker.toml` | 决定版本来源；仓库规则要求它与 PKGBUILD 由 CI 同推 |
| 符号链接 / 子模块（mode `120000` / `160000`）、`.git/` 下任何路径、绝对路径、`..` | `git apply` 照做，但打包改动永远不需要 |

`pkgver=` / `pkgrel=` 由 CI 写，`check` 会拒绝（见硬规则）。

## 自检

- 改过 Issue Form 的字段 `label`、`options` 或 `multiple` 后，必须跑 `check-form`（用 `yq` 解析表单），
  否则解析静默失效：

```sh
python3 scripts/mod-request.py \
  check-form --form .github/ISSUE_TEMPLATE/mod-request.yml
```

- 常见失败：`target_version` 带了 `pre` 后缀；选了 `lite`（本仓库还没有 lite 包）；
  `payload` 缺包或多包（选了没写、写了没选）；diff 改了 `pkgver`/`pkgrel`；
  `message` 缺失或以空行开头；内容里出现 4 个以上反引号的行；`summary`/`evidence` 里出现行首
  `### `（会把正文切成两段）；正文超过 65536 字符（GitHub 上限，diff 大时要拆成两条请求）。
