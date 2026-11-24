# hermes-backup

Hermes Agent 的**备份 / 校验 / 还原 / 迁移**工具。单文件、零依赖、只使用 Python 标准库（3.8+），Windows / macOS / Linux 通用。

Hermes 的数据分散在用户目录里，而且没有官方迁移命令。这个工具回答一个问题：**换电脑、重装、升级翻车时，怎样把该留的留下，把没用的扔掉。**

## 快速开始

```bash
# 直接运行，不需要安装
python hermes_backup.py backup --profile data
```

Windows 上也可以用自带的 Python：

```powershell
python .\hermes_backup.py backup --profile data --out D:\backup\hermes-20261124.zip
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `backup` | 打包指定 profile 的数据 |
| `verify` | 按 manifest 逐文件校验 sha256 |
| `list` | 不解压，查看包里有什么 |
| `restore` | 解包到目标目录（默认自动识别 Hermes home） |
| `selftest` | 离线跑一次完整往返测试 |

### backup

```bash
python hermes_backup.py backup --profile data                    # 默认：配置/会话/记忆/技能/插件
python hermes_backup.py backup --profile data --with-secrets     # 额外包含 auth.json
python hermes_backup.py backup --profile source                  # 只打包源码仓库（含 .git）
python hermes_backup.py backup --profile full --no-hash          # 全量，跳过哈希更快
python hermes_backup.py backup --profile data --dry-run          # 只估算，不写文件
```

### verify / list

```bash
python hermes_backup.py verify hermes-data-20261124.zip
python hermes_backup.py list hermes-data-20261124.zip
```

### restore

```bash
python hermes_backup.py restore hermes-data-20261124.zip --into /path/to/staging --dry-run
python hermes_backup.py restore hermes-data-20261124.zip --force
python hermes_backup.py restore hermes-data-20261124.zip --only sessions
```

## profiles

| profile | 内容 | 体积量级 | 用途 |
| --- | --- | --- | --- |
| `data` | `config.yaml`、`state.db`、`sessions/`、`memories/`、`skills/`、`plugins/`、`cron/` 等 | 几十 MB | **默认**，日常备份与换机核心 |
| `source` | `hermes-agent/`（含 `.git`） | 数百 MB | 新机免掉最慢的一次 clone |
| `full` | 上述两者 | 两者之和 | 整机搬家 |

永远**不**打包：`cache/`、`logs/`、`tools/`、`node/`、`runtime/`、`bootstrap-cache/`、`installs/`、`terminal-sessions/`、各 `.venv`、`__pycache__`、`node_modules`。它们要么是缓存，要么由安装器重新生成。

## 安全

- 默认**不含** `auth.json`（里面有 API Key / Token）。需要时加 `--with-secrets`，命令行会给出警告。
- 归档里的 `manifest.json` 记录了每台机器的 hostname 和路径；如果不希望泄露，用 `--no-hash` 之外没有开关——可以在解压后自行删除该字段，或提 issue 加 `--anonymize`。
- `restore` 会做路径穿越防护，只接受相对路径。

## 包结构

```
hermes-data-20261124.zip
├── manifest.json      # 工具版本、profile、hermes 版本、每个文件的 sha256
├── config.yaml
├── state.db
└── sessions/...
```

## 还原之后

数据还原只解决"我的东西还在"。可执行文件、Python 环境、服务注册需要重新安装：

1. 先退出 Hermes（包括托盘图标），否则 Windows 上数据库文件可能被占用。
2. 还原数据到 `%LOCALAPPDATA%\hermes`（Windows）或 `~/.hermes`。
3. 运行官方安装器，它会补齐 venv、tools 和网关服务。
4. 启动 Hermes，确认会话与配置已在。

## 已知限制

- **不会**在运行中安全地复制 SQLite WAL：建议先退出 Hermes 再备份，否则 `state.db` 可能缺少最近的写入。
- 备份不会加密。含密钥的包请自行保管。
- `source` profile 依赖完整 `.git`；如果仓库是浅克隆，新机上的 `hermes update` 仍可能需要联网补历史。

## 许可

MIT，见 [LICENSE](LICENSE)。
