# cpa-warden

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![uv](https://img.shields.io/badge/deps-uv-6f42c1)

[English](README.md)

`cpa-warden` 是一个基于 CPA 管理接口的交互式账号维护工具，核心依赖两类接口：

- `GET /v0/management/auth-files`
- `POST /v0/management/api-call` -> `GET https://chatgpt.com/backend-api/wham/usage`

脚本会先读取远程认证文件列表，再把本地状态写入 SQLite，随后并发探测 `wham/usage`，将账号分类为：

- `401` 失效
- `quota limited` 限额
- `recovered` 从之前的限额禁用状态恢复

## 项目状态

这个项目已经可以用于本地 CPA 账号运维，目前正在继续整理成更完整的开源仓库。

当前重点：

- 稳定 `scan` 流程
- 更安全的 `maintain` 流程
- 彻底外置敏感配置
- 保持生产模式输出简短、日志可追踪

## 功能特性

- 敏感信息只从外部配置文件读取，不写在代码里
- 默认支持交互式运行
- `scan` 模式用于检测和导出
- `maintain` 模式用于删除 / 禁用 / 恢复启用
- 并发探测 `wham/usage`
- 使用 SQLite 保存本地状态
- 生产模式终端输出简短，支持 Rich 进度条
- `debug` 模式输出详细调试信息，并同步写入日志文件

## 判定规则

- `401`：`unavailable == true` 或 `api-call.status_code == 401`
- `quota limited`：`api-call.status_code == 200` 且 `body.rate_limit.limit_reached == true`
- `recovered`：此前被脚本标记为 `quota_disabled`，且本轮 `allowed == true`、`limit_reached == false`

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## 安装依赖

```bash
uv sync
```

## 配置方式

不要把敏感信息写进代码。先复制示例配置：

```bash
cp config.example.json config.json
```

然后编辑 `config.json`，至少填写：

- `base_url`
- `token`

`config.json` 已加入 `.gitignore`，不要提交到仓库。

示例：

```json
{
  "base_url": "https://your-cpa.example.com",
  "token": "replace-with-your-management-token",
  "target_type": "codex",
  "provider": "",
  "probe_workers": 40,
  "action_workers": 20,
  "timeout": 15,
  "retries": 1,
  "quota_action": "disable",
  "delete_401": true,
  "auto_reenable": true,
  "db_path": "cpa_warden_state.sqlite3",
  "invalid_output": "cpa_warden_401_accounts.json",
  "quota_output": "cpa_warden_quota_accounts.json",
  "log_file": "cpa_warden.log",
  "debug": false,
  "user_agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"
}
```

## 使用方式

交互式运行：

```bash
uv run python cpa_warden.py
```

命令行运行：

```bash
uv run python cpa_warden.py --mode scan
uv run python cpa_warden.py --mode scan --debug
uv run python cpa_warden.py --mode maintain
uv run python cpa_warden.py --mode maintain --quota-action delete --yes
uv run python cpa_warden.py --mode maintain --no-delete-401
```

## 运行模式

### `scan`

该模式会：

- 拉取所有 auth 文件
- 并发探测 `wham/usage`
- 更新本地 SQLite 状态库
- 导出当前轮的 `401` 和限额账号

### `maintain`

该模式会先执行 `scan`，然后继续执行动作：

- 按配置删除 `401` 账号
- 对限额账号执行禁用或删除
- 对已恢复账号执行重新启用

## Roadmap

- 继续完善失败原因统计和错误分类
- 增加 probe 分类与动作流程的自动化测试
- 增加 CI 做基础检查和冒烟验证
- 增强导出和汇总报告能力
- 持续优化开源文档和上手体验

## 输出文件

- `cpa_warden_state.sqlite3`：本地状态数据库
- `cpa_warden_401_accounts.json`：当前轮 `401` 导出
- `cpa_warden_quota_accounts.json`：当前轮限额导出
- `cpa_warden.log`：运行日志

## 日志与输出

- 生产模式终端输出尽量简短
- 如果终端支持 TTY，生产模式会优先显示 Rich 进度条
- `--debug` 或 `debug: true` 会在终端打印更详细的调试信息
- 日志文件始终保留完整的调试级别信息

## 项目结构

- `cpa_warden.py`：主脚本
- `clean_codex_accounts.py`：旧命令兼容包装脚本
- `config.example.json`：示例配置
- `pyproject.toml`：`uv` 项目配置和依赖

## 安全说明

- 不要提交 `config.json`
- 不要提交真实 token 或真实账号标识
- 如果导出文件和日志包含生产数据，只应在本地保存

## 贡献说明

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 更新记录

见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

MIT，详见 [LICENSE](LICENSE)。
