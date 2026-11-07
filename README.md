# Nori

**无需 clone 安装：** 下载私有 CI 的 `nori-linux-wsl2` 安装包，执行 `python3 nori.pyz --install`。详见 [安装、更新与卸载](docs/INSTALL.md)。需 Linux/WSL2 和 Python 3.11+，不支持原生 Windows。

Nori 是本地、串行的工作控制框架：固定需求与计划，安排执行工序，持续监督，再根据独立证据决定是否交付。实现以《Agent Factory 1.0 系统架构与详细方案 v0.1》为基线。

**当前状态：V1 框架功能已实现。本轮按用户要求只用合成任务和替身工具验收，不要求真实模型联调。** Codex 与 Claude Code 的官方 CLI 适配器已有实现。当前测试机 Codex 已安装并报告登录，但实际合成任务因其内部初始化遇到只读文件系统而退出；Claude CLI 未安装。这些真实环境情况不作为本轮模拟验收的阻碍，也不冒充真实模型联调通过。

## 当前产品边界

这轮优先完善面向项目经理的网页原型：以 XMind 式层级脑图为主，展示需求、工作包与实际验收项，配合执行器、风险、验收证据与待决策事项。父子线代表需求分解，执行依赖单独标识。默认交互原型以“Nori 自我迭代”为示例，展示需求→工作包→子任务的完整视觉、示例指派、风险和验收。示例不会启动 Agent 或修改 Nori 自身。自然语言输入仍真实保存为需求草稿，不假装已经完成任意任务的自动规划；未接入功能明确标注。现有模拟项目仍能真实完成批准、执行、返工和独立验收。高级结构化计划入口保留，不要求首次使用者先写 JSON。

## 浏览器界面

安装后运行 `~/.local/bin/nori`（若已在 PATH 中则 `nori`），默认启动本地 Web 界面并尝试打开浏览器。WSL 未能自动打开 Windows 浏览器时，复制启动时打印的本机地址到浏览器。

界面可保存原始需求，展示工作包、依赖与执行器，批准并运行模拟项目，查看风险、证据及待决策事项。模拟计划来自明确标注的固定示例；任意需求的自动拆解和交互式 Agent 指派尚未接通。已有项目的高级结构化计划编辑仍可用。首次使用选择合成示例，不需真实项目。它不是聊天窗口，不会把一句自然语言自动变成已批准的执行计划。

仅监听 `127.0.0.1`；不要把端口转发到公网。项目保存在 `~/.local/share/nori-web`，与程序安装位置分离。用 Ctrl+C 关闭 Web 服务不会删除项目，也不等于确认所有执行器已经停止；先在界面暂停并确认退出。

高级启动：`nori web --port 8765 --no-browser`。界面使用每次启动生成的访问令牌，请使用程序打印的完整 URL，不分享给他人。原终端命令保留为高级管理和诊断入口。

## 运行与验收

Python 3.11+，Linux / WSL；只用标准库，无需安装第三方 Python 包。不支持原生 Windows；WSL2 的实际桌面打开体验仍需目标机验证。

```sh
python -m unittest discover -s tests -v
python -m compileall -q nori tests
python -m nori demo /tmp/nori-first-demo
```

`demo` 必须用新目录，只写临时文本：A 漏一行却自报完成 → 验收失败 → B 被阻止 → A 新尝试补齐 → B 读取 A → 整体验收通过。没有真实项目、SVN、部署或模型费用。

另有同一端到端测试：Codex 替身漏项并自报完成，独立验收失败；确认退出后关闭并重开控制会话，由 Claude 替身保留部分成果接手返工；依赖任务此前持续阻塞，修复通过后才执行，最终整体验收通过。原需求/计划版本及首次失败记录均保留。

完整日常入口不局限于 demo：

```sh
python -m nori example > /tmp/nori-spec.json
python -m nori init /tmp/nori-control --workspace /tmp/nori-work --spec /tmp/nori-spec.json
# 检查规范，使用 init 返回的 approval_digest 批准该内容：
python -m nori approve /tmp/nori-control --digest <approval_digest>
python -m nori run /tmp/nori-control --adapter mock
python -m nori status /tmp/nori-control
```

规范含目标、稳定要求编号、任务原因、依赖、读写范围、独立验收项、尝试/时间预算、禁止事项和允许操作。`examples/two_text_tasks.json` 可作为输入示例。`write_scope` 支持精确文件和以 `/` 结尾的目录；共享写入位置必须有明确依赖。模型自述不能改变批准版本、正式状态或验收结论。

## 已实现的 V1 路径

- **需求与计划**：导入、摘要批准、版本化修订、理由和历史；小范围计划变更不得改目标、验收、操作权限或预算。重要变化必须显式批准新摘要，保留旧失败。
- **串行执行**：依赖就绪队列、阻塞原因、唯一运行许可、固定输入/基线/版本、持久启动意图、命令去重、旧结果拒绝、有限调度步数。
- **双适配器**：Claude Code / Codex 官方 CLI 参数，探测、持久进程监督、日志、退出、观察、停止和恢复；只用 argv，不拼 shell，不绕过工具审批。
- **过程监督**：运行中范围检查、时间和输出预算、缺少进展证据提醒、次数上限。没有文件改动本身不会被当成无效测试或停滞。
- **独立验收**：精确文本、JSON Pointer 值、预先批准的固定命令、人工检查；三态结果与原始输出；任务和整体验收分开；依据/产物变化使证据失效。
- **暂停和继续**：停止请求不等于退出，不释放未确认的许可；确认结束后可检查、返工、切换工具。取消与部分交付不会标成技术完成。
- **状态恢复**：JSON 唯一真相，双层 OS 锁和线程锁、修订校验、fsync/校验和/上一版备份/原子替换；显式恢复后先盘点所有运行与实际文件，不猜测重跑。
- **用户入口**：本地浏览器界面、终端高级命令与派生 Markdown 进度/验收报告。调度器在步骤间释放控制锁，另一终端能查询、暂停或取消。

常用命令：

```sh
python -m nori probe /tmp/nori-control --adapter codex
python -m nori probe /tmp/nori-control --adapter claude
python -m nori run /tmp/nori-control --adapter codex --max-steps 30
python -m nori pause /tmp/nori-control
python -m nori reconcile /tmp/nori-control
python -m nori resume /tmp/nori-control
python -m nori finalize /tmp/nori-control
python -m nori --help
```

每个修改命令可传 `--command-id` 和 `--revision`。省略时，CLI 在独占锁内读取当前修订并生成新编号；重试同一请求应复用原编号。

## 重要边界

- 验收只使用临时合成任务，未操作真实游戏/迁移项目。文本或 JSON 反例证明控制路径有效，不等于通用代码语义正确。
- 框架不自行安装、登录或授予 Claude/Codex 权限。登录、模型访问、具体版本能力和真实沙箱仍以本机探测/联调为准；不自动换工具。
- Linux 进程组停止和合作式范围检查不是恶意同账户代码的硬隔离。注册命令可能有副作用，必须在用户批准的规范内；没有网络/远端系统的通用强隔离。
- Windows 原生、真实业务集成、真实双模型完整切换尚未验收。WSL/Linux 是本版实现宿主，不提供未经验证的跨 Windows/WSL 调度。
- 命令验收首次生成文件会得到 `INCONCLUSIVE`；检查允许的产物后重新固定候选，稳定重跑才能通过。变化无常的输出不能当作稳定证据。
- 损坏状态默认阻塞。备份恢复不是回滚工作文件，也不等于可以立即重跑。未知进程、缺失日志或无法匹配的运行仍需核对。
- 已测试真实子进程强杀和写入故障，不宣称覆盖任意硬件掉电。
- 不含并行、多 Agent 团队、模型路由、Web 平台、自动发布或长期无人值守。

详见 [V1 操作与验收](docs/V1.md)、[适配器说明](docs/ADAPTERS.md)、[P1 历史说明](docs/P1.md)。
