# hermes-backup

[![ci](https://github.com/ZjusUUUXIANGCAI/Hermes-backup-tool/actions/workflows/ci.yml/badge.svg)](https://github.com/ZjusUUUXIANGCAI/Hermes-backup-tool/actions/workflows/ci.yml)

Hermes Agent 的**备份 / 校验 / 还原 / 迁移**工具。单文件，默认零依赖（Python 3.8+），Windows / macOS / Linux 通用。

## 为什么还需要它

官方负责"能跑"，这个工具负责"能搬、能查、不漏"。

| 对比项 | 手工拷贝 `%LOCALAPPDATA%\hermes` | 本工具 |
| --- | --- | --- |
| 体积 | 4.9 GB（含 cache / tools / venv） | 66 MB 数据，压完约 19 MB |
| 凭据 | `auth.json`、平台 token 一起带走 | 默认跳过 8 类凭据路径，配置值脱敏 |
| 一致性 | 运行中直接拷 SQLite，可能拷到半截 | SQLite backup API 快照 + integrity_check |
| 完整性 | 无 | 每文件 sha256，审计重复/缺失/多余条目 |
| 加密 | 无，压缩包即明文 | v2：AES-256-CTR + HMAC；错口令直接拒绝 |
| 换机速度 | 要重拉最慢的 clone | `source` profile 保留 `.git`，免 clone |

## 直接运行（免安装）

| 平台 | 文件 | 用法 |
| --- | --- | --- |
| Windows x64（单文件，推荐） | `hermes-backup-0.3.1-win64.exe` | 直接运行，免解压（在 exe 同目录解压临时文件） |
| Windows x64（目录版） | `hermes-backup-0.3.1-win64.zip` | 解压，运行 `hermes-backup\hermes-backup.exe` |
| macOS / Linux / WSL | `hermes-backup.sh` + `hermes_backup.py` | 同一目录下 `chmod +x hermes-backup.sh`，然后 `./hermes-backup.sh --help` |
| 任意平台（源码） | `hermes_backup.py` | `python hermes_backup.py --help` |

Windows 包内置 Python 与 `cryptography`，v1/v2 加密都能直接用；exe 未做代码签名，首次运行 SmartScreen 会提示，选"更多信息 → 仍要运行"。

双击 exe（不带参数）会打印帮助并等待回车，不会一闪而过；脚本里想跳过等待可设 `HERMES_BACKUP_NO_PAUSE=1`。

下载：<https://github.com/ZjusUUUXIANGCAI/Hermes-backup-tool/releases/latest>

## 安全模型（默认不含敏感内容）

1. **排除凭据类路径**：`auth.json`、`gateway-connections.json`、`gateway_state.json`、`channel_directory.json`、`platforms/`、`pairing/`、`bot_relay/`。
2. **脱敏配置**：`config.yaml`、`*.env` 里形如 `api_key: sk-...`、`token: ...` 的值替换为 `<<REDACTED>>`；布尔、数字、`${ENV}` 引用保持原样。
3. **不打包缓存**：`cache/`、`logs/`、`tools/`、`node/`、`runtime/`、`.venv`、`*.lock`、`*.pid`。

## 数据一致性

- **SQLite**：`state.db`、`kanban.db`、`projects.db`、`cron/executions.db` 先通过 backup API 快照，跑 `PRAGMA integrity_check`，通过后才归档；WAL 内容一并进入快照。需要保留原始文件形态时用 `--no-db-snapshot`。
- **竞态**：归档是边写边算哈希的单趟流程，manifest 描述的就是压缩包里真实的字节，不存在"算完哈希又被改"的窗口。
- **路径**：创建、校验、还原三处都做统一校验，拒绝盘符、UNC、反斜杠、`..`/`.`、控制字符、Windows 保留设备名、结尾点/空格。

## 审计规则（`verify` / `restore` 都会跑）

- 重复 ZIP 条目（大小写不敏感）与重复 manifest 路径
- manifest 记录但包里缺失、包里存在但 manifest 未登记
- 大小不符、sha256 不符、路径不安全
- `totals.files` / `totals.bytes` 与条目不一致
- 读取时由 zipfile 自动校验 CRC

任何一条不过，`verify` 返回非零，`restore` 直接拒绝。

## 加密

`--with-secrets` 会包含凭据文件，并**强制要求加密**：

```bash
python hermes_backup.py backup --profile data --with-secrets --encrypt
```

- **v2（默认）**：PBKDF2-HMAC-SHA256（210,000 次）+ AES-256-CTR + HMAC-SHA256（encrypt-then-MAC）。需要可选依赖 `cryptography`；未安装时会明确提示。
- **v1**：纯标准库的 HMAC-SHA256 密钥流，适合不能装依赖的环境；v2 的读写都可随时切回 `--crypto v1`。
- 口令来源：交互输入、`--password-env VAR`、`--password-file FILE`。KDF 迭代次数被限制在 5 万～200 万，口令长度上限 1024。
- 两种格式的包都能被新版读取；错口令或密文被改动会在解密阶段拒绝。

## 快速开始

```bash
python hermes_backup.py backup --profile data --out D:\backup\hermes.zip
python hermes_backup.py verify D:\backup\hermes.zip
python hermes_backup.py list   D:\backup\hermes.zip
python hermes_backup.py restore D:\backup\hermes.zip --into D:\staging --dry-run
python hermes_backup.py selftest
```

### profiles

| profile | 内容 | 体积量级 |
| --- | --- | --- |
| `data` | 配置、数据库、会话、记忆、技能、插件 | 几十 MB |
| `source` | `hermes-agent/`（含 `.git`） | 数百 MB |
| `full` | 两者 | 两者之和 |

### backup 选项

| 选项 | 说明 |
| --- | --- |
| `--out PATH` | 输出路径；加密时后缀为 `.hbz` |
| `--with-secrets` | 包含凭据（必须同时 `--encrypt`） |
| `--encrypt` / `--crypto v1\|v2` | 口令加密与加密格式 |
| `--password-env VAR` / `--password-file F` | 非交互提供口令 |
| `--no-db-snapshot` | 不做 SQLite 一致性快照 |
| `--no-scrub` | 关闭配置脱敏 |
| `--anonymize` | manifest 里隐藏主机名和 home 路径 |
| `--exclude GLOB` | 额外排除，可重复 |
| `--dry-run` / `--force` | 只估算 / 覆盖已有输出 |

### restore

```bash
python hermes_backup.py restore hermes.zip --into /tmp/new-home --dry-run
python hermes_backup.py restore hermes.zip --only sessions --force
python hermes_backup.py restore hermes.hbz --password-env HB_PASS
```

还原前会先跑完整审计；默认拒绝覆盖已有文件；默认不穿越符号链接/ junction。

## 兼容性

- 归档 manifest 格式 1（0.2.x）与 2（0.3.x）都能读。
- 加密 v1 与 v2 都能读；写哪个由 `--crypto` 决定。
- Python 3.8+；`cryptography` 仅在 v2 加密时需要。

## 已知限制

- 脱敏是启发式正则，不是内容审计；自定义插件把密钥藏进别的文件名时不会被发现。
- `--no-db-snapshot` 下不保证 SQLite 一致性。
- 加密没有硬件密钥或密钥托管；口令丢失即无法恢复。
- `source` profile 依赖完整 `.git`；浅克隆在新机上仍可能需要联网补历史。

## 开发与测试

```bash
python -m unittest discover -s tests   # 28 项：路径、审计、加密、SQLite、备份/恢复
python hermes_backup.py selftest
```

CI 在 Ubuntu 22.04 与 Windows 上跑 Python 3.8–3.13，其中一个任务安装 `cryptography` 以覆盖 v2 路径。

## 许可

MIT，见 [LICENSE](LICENSE)。
