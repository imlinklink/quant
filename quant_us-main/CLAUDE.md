# quant_us-main 项目说明

本项目的长期目标、用户偏好、买入/卖出优化方向，**唯一来源**是同一目录下的 `AGENTS.md`：

@AGENTS.md

---

## 为什么这里只有一行导入

`AGENTS.md` 是跨工具约定，Claude Code 在「项目里没有 `CLAUDE.md`」时才会读它
（默认设置 `claude-md-or-agents-md`）。一旦本文件存在，`AGENTS.md` 就**不再被直接加载**，
由上面的导入负责把它带进来 —— 于是：

- 内容**只有一份定义**（`AGENTS.md`），改它即生效，不会出现两个文件说法不一致；
- 这个仓库出过多次「同一件事两份定义、各自漂移」的真 bug（退出原因枚举、角色清单、
  `VALID_ROLES`、`config.yaml` 与记录文件…），所以**刻意不抄一份**。

**不要**把 `AGENTS.md` 的内容复制到本文件里。要改内容，改 `AGENTS.md`。

若某天发现 `AGENTS.md` 的内容没有生效（例如换了不支持 `AGENTS.md` 的版本、或走了
第三方 provider 不取 feature flag），退路是在 `~/.claude/settings.json` 里设

```json
{ "pluginConfigs": { "agents-md@builtin": { "options": {
  "instructionFiles": "claude-md-and-agents-md" } } } }
```

让两份都被读；而**不是**把它抄成本文件的内容。
