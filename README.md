# hermes-backup

Hermes Agent 的**备份 / 校验 / 还原 / 迁移**工具。单文件、零依赖、只用 Python 标准库（3.8+），Windows / macOS / Linux 通用。

## 安全模型（默认不含敏感内容）

默认档位做三件事：

1. **排除凭据类路径**：`auth.json`、`gateway-connections.json`、`gateway_state.json`、`channel_directory.json`、`platforms/`、`pairing/`、`bot_relay/`。
2. **脱敏配置**：`config.yaml`、`*.env` 里形如 `api_key: sk-...`、`token: ...` 的值替换为 `<<REDACTED>>`；布尔值、数字、`${ENV}` 引用保持原样，避免破坏还原后的行为。
3. **不打包缓存**：`cache/`、`logs/`、`tools/`、`node/`、`runtime/`、`*.venv`、`*.lock`、`*.pid` 全部跳过。

## 需要连密钥一起带走时

`--with-secrets` 会包含凭据文件，并且**强制要求加密**：

```bash
python hermes_backup.py backup --profile data --with-secrets --encrypt
# 输出 hermes-backup-data-20261124-1930.hbz
```

口令可以用三种方式给：交互输入（两次确认）、`--password-env VAR`、`--password-file FILE`。

加密方案（纯标准库，无第三方依赖）：

- 口令派生：**PBKDF2-HMAC-SHA256**，210000 次迭代，64 字节
- 加密：HMAC-SHA256 计数器密钥流（CTR 式），XOR 加密
- 完整性：**encrypt-then-MAC**，对整个密文做 HMAC-SHA256，错误口令在解密时即被拒绝

> 这是一个自定义构造，不是 age/GPG 那种经过长期审计的标准格式。对"防止备份包被随手打开"够用；如果你要对抗更强的对手，请改用 `age` 或 `gpg` 再包一层。

确实要明文带凭据（不推荐）时：`--with-secrets --allow-plaintext-secrets`。

## 快速开始

```bash
python hermes_backup.py backup --profile data --out D:\backup\hermes.zip
python hermes_backup.py verify D:\backup\hermes.zip
python hermes_backup.py list   D:\backup\hermes.zip
python hermes_backup.py restore D:\backup\hermes.zip --into D:\staging --dry-run
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `backup` | 打包指定 profile |
| `verify` | 按 manifest 逐文件校验 sha256 |
| `list` | 不解压查看内容 |
| `restore` | 解包到目标目录（默认自动识别 Hermes home） |
| `selftest` | 离线往返测试（含加密、脱敏、错误口令） |

### profiles

| profile | 内容 | 体积量级 |
| --- | --- | --- |
| `data` | 配置、数据库、会话、记忆、技能、插件 | 几十 MB |
| `source` | `hermes-agent/`（含 `.git`） | 数百 MB |
| `full` | 两者 | 两者之和 |

### backup 常用选项

| 选项 | 说明 |
| --- | --- |
| `--out PATH` | 输出路径；加密时后缀为 `.hbz` |
| `--with-secrets` | 包含凭据（必须同时 `--encrypt`） |
| `--encrypt` | 口令加密 |
| `--password-env VAR` / `--password-file F` | 非交互提供口令 |
| `--allow-plaintext-secrets` | 允许明文带凭据（危险） |
| `--no-scrub` | 关闭配置脱敏 |
| `--anonymize` | manifest 里隐藏主机名和 home 路径 |
| `--no-hash` | 跳过 sha256，更快但校验变弱 |
| `--exclude GLOB` | 额外排除，可重复 |
| `--dry-run` | 只估算不写文件 |
| `--force` | 覆盖已存在的输出 |

### restore

```bash
python hermes_backup.py restore hermes.zip --into /tmp/new-home --dry-run
python hermes_backup.py restore hermes.zip --only sessions --force
python hermes_backup.py restore hermes.hbz --password-env HB_PASS
```

还原有路径穿越防护；默认拒绝覆盖已有文件；加密包先校验 MAC 再写盘。

## 包结构

```
hermes-backup-data-20261124.zip
├── manifest.json      # 工具版本、profile、hermes 版本、每文件 sha256、是否含密钥
├── config.yaml
├── state.db
└── sessions/...
```

## 还原之后

数据只是数据；可执行文件、Python 环境、服务注册要重新安装：

1. 退出 Hermes（含托盘），Windows 上数据库可能被占用；
2. 还原到 `%LOCALAPPDATA%\hermes`（Windows）或 `~/.hermes`；
3. 跑官方安装器补齐 venv / tools / 网关服务；
4. 启动确认会话与配置还在。

## 已知限制

- 不处理 SQLite WAL 的在线一致性：建议先退出 Hermes 再备份。
- 脱敏是启发式正则，不是内容审计；自定义插件把密钥藏进别的文件名时不会被发现。
- 加密是自实现构造（见上），未做侧信道加固。
- `source` profile 依赖完整 `.git`；浅克隆在新机上仍可能需要联网补历史。

## 许可

MIT，见 [LICENSE](LICENSE)。
