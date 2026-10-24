# agent-shared-brain

> 两个 AI agent，一个大脑。用 git 同步记忆、留言和交接单，带篡改检测。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#)

**English** | [English README](README.en.md)

---

## 它解决什么问题

当你在两台机器上跑同一个 AI agent（比如笔记本 + 一台常开的服务器），会遇到三件事：

1. **关机就失忆**：笔记本合上，另一个 agent 不知道你刚才在做什么。
2. **各记各的**：两份记忆慢慢分叉，改过的一条在另一边还留着旧版本，互相矛盾。
3. **不敢合并**：两个 agent 同时写同一个文件，git 冲突、内容丢失、谁覆盖了谁说不清。

这个项目用一套很土但很硬的办法解决：**每个 agent 只往自己的追加日志里写操作，谁也不碰谁的文件**，然后由确定性算法合成一份记忆。

- 不会冲突（结构性保证，不是靠重试）
- 改过就是改过（显式 `update`，不会新旧并存）
- 删了就是删了（`forget` 置作废时间，历史仍可查）
- 被改过历史能查出来（链式锚点）

零依赖，纯 Python 标准库 + git。

## 30 秒开始

```bash
# 1. 建一个私有仓库当"共享大脑"，两台机器各克隆一份
git clone git@github.com:you/brain.git ~/.hermes/brain
printf 'alpha\n' > ~/.hermes/brain/.agent-id      # 这台机器叫 alpha，那台叫 beta

# 2. 从现有记忆初始化（把 MEMORY.md / USER.md 拆成一条条事实）
python3 ~/.hermes/brain/sync/facts.py migrate

# 3. 每 10 分钟跑一次
sh ~/.hermes/brain/sync/brain_sync.sh
```

第二台机器只做第 1 步（`.agent-id` 写 `beta`），然后跑一次 `brain_sync.sh` —— 它会渲染出和第一台完全一样的记忆。

定时任务（二选一）：

```bash
# Linux / 服务器
*/10 * * * * XDG_RUNTIME_DIR=/run/user/$(id -u) sh $HOME/.hermes/brain/sync/brain_sync.sh

# macOS
# examples/ai.agent.brain.plist 丢进 ~/Library/LaunchAgents/ 即可
```

## 工作原理

```
        alpha 的机器                        beta 的机器
   ┌──────────────────┐              ┌──────────────────┐
   │ facts/alpha.jsonl│              │ facts/beta.jsonl │
   │   add/update/... │              │   add/update/... │
   └────────┬─────────┘              └─────────┬────────┘
            │ 只追加，永不改写                  │
            └──────────► git 仓库 ◄─────────────┘
                            │
                  确定性重放（fold）
                            │
              ┌─────────────┴─────────────┐
        MEMORY.md / USER.md        mailbox/ journal/ pending/
        （每轮注入的"热记忆"）      （留言、日志、交接单）
```

四条规则撑起全部：

| 规则 | 为什么 |
|---|---|
| 每个 agent 只写 `facts/<自己>.jsonl` | 两台永远不碰同一个文件 → 结构上没有冲突 |
| 操作是 `add` / `update` / `forget` / `restore` / `pin` / `use` | 改和删是显式动作，不靠猜 |
| 渲染是三方合并（base / live / facts） | 只有"上次我渲染的"和"现在磁盘上的"不一致，才算你改过；否则落后的一台会把自己的旧副本吸回去 |
| `chain.py` 给日志加锚点 | 历史被改写或截断时立刻发现（GitHub 私有仓的 branch protection 要付费，这个不要） |

### 为什么不能只用并集

最早的版本是"两份记忆取并集"。它有两个治不好的病：改动要靠相似度猜（猜错就新旧两版并存），删除无法表达（并集分不清"被删了"和"这台还没同步到"）。于是出现"你明明改了一条，下次它又回来了"。现在的做法把这两件事变成显式操作，并把"上次渲染结果"当作合并基准。

细节和踩过的坑见 [docs/design.md](docs/design.md)。

## 命令

```bash
# 记忆
facts.py sync                       # 拉取 + 三方合并 + 渲染 + 推送
facts.py list [--all] [--scope user]
facts.py add "一句话事实" [--scope user]
facts.py forget <id | 子串>          # 子串 ≥6 字；多条需 --yes；pinned 需 --force
facts.py restore <id>
facts.py pin "子串"                  # 钉住：预算挤不掉、不会被自动删
facts.py history <id>                # 一条事实的完整变更史
facts.py search "关键词" [--all]     # FTS5 全文检索（含已作废）

# 通信
mailbox.py send "内容" --to beta --kind progress
mailbox.py inbox --unread
mailbox.py ack <msg-id>
mailbox.py log "今天做了什么"
mailbox.py pending add "这件事交给你" --for beta

# 完整一轮（定时任务调这个）
sh sync/brain_sync.sh

# 篡改检测
chain.py verify | update | accept | status
```

## 接入 AI agent

`skills/shared-brain/SKILL.md` 是一份可以直接给 agent 加载的操作说明（触发条件、命令、什么时候该留言、什么不能做）。Hermes Agent 用户把它放进 `~/.hermes/skills/`；其他框架把内容塞进 system prompt 也行。

## 适用与不适用

**适合**

- 两台（或几台）机器跑同一个 agent，需要共享长期记忆
- 想让"关机期间的进展"能被另一台接上
- 想审计"这条记忆什么时候被谁改的"

**不适合**

- 需要毫秒级同步（这里是分钟级，最终一致）
- 几十个 agent 的高并发写入（要换成真正的共识协议）
- 把记忆当数据库查（它只做"每轮注入的热记忆"，冷数据用 `search`）

## 已知限制

- 相似度配对（判断"改写"还是"删+加"）是启发式，阈值 0.72；大幅改写会记成 `forget + add`，历史串不起来
- 预算淘汰按"最后修改时间"，`use` 操作目前没有自动触发点
- 不是实时：两台之间最长 10 分钟延迟

## 许可

MIT。见 [LICENSE](LICENSE)。
