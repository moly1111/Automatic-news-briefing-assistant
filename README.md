# 📰 Automatic News Briefing Assistant

每日自动新闻简报系统 — 搜集四大领域新闻，AI 深度分析，自动推送订阅。

## 工作流

```
权限词轮换 → 生成简报(含昨日回顾) → 格式验证 → 群发订阅 → 更新记录
```

## 功能

| 模块 | 说明 |
|------|------|
| 订阅推送 | 每日 9:00 向订阅列表群发简报 |
| 即时触发 | 订阅者发送指定关键词邮件 → 自动回复最新简报 |
| 权限词轮换 | 每日从 BIP39 词库随机选取鉴权词 |
| 格式验证 | 5 项 Markdown 检查 + 自动修复 |
| 昨日回顾 | 每期简报回溯前一日判断，修正或延续 |

## 快速开始

### 1. 克隆

```bash
git clone https://github.com/moly1111/Automatic-news-briefing-assistant.git
cd Automatic-news-briefing-assistant
```

### 2. 安装依赖

```bash
pip install markdown
```

### 3. 配置

```bash
cp .env.example .env
# 编辑 .env 填入真实邮件凭据
```

| 变量 | 说明 |
|------|------|
| `NEWS_SENDER_EMAIL` | 发件邮箱 |
| `NEWS_SENDER_PASSWORD` | SMTP 授权码 |
| `NEWS_RECEIVER_EMAIL` | 管理员邮箱 |
| `NEWS_EMAIL_ACCOUNT` | 监控邮箱（POP3） |
| `NEWS_EMAIL_PASSWORD` | POP3 授权码 |
| `NEWS_ADMIN_EMAIL` | 管理员通知邮箱 |

### 4. 配置订阅者

```bash
cp subscribers.txt.example subscribers.txt
# 编辑 subscribers.txt，每行一个邮箱
```

### 5. 配置鉴权词（可选）

编辑 `auth_keyword.txt`：
- 第 1 行：主题鉴权词（每日轮换）
- 第 2 行：正文鉴权词（固定）

## 使用

```bash
# 权限词轮换
python3 rotate_keyword.py

# 验证简报格式（自动修复）
python3 validate_report.py reports/daily-2026-06-28.md --repair

# 群发订阅（预览）
python3 send_to_subscribers.py --dry-run

# 群发订阅（正式）
python3 send_to_subscribers.py

# 启动邮件监听（即时触发）
python3 watch_email.py
```

## 简报结构

每期简报包含：

- **〇 昨日预判回顾** — 逐条核实前一日判断
- **一 一句话总结** — 与昨日形成对照
- **二 四大领域关键事件** — AI/科技、财经/金融、国际/地缘、产业/商业
- **三 深层因果分析** — M1 逻辑链
- **四 大势判断** — M7 大势思维
- **五 风险仪表盘** — 对比昨日状态
- **六 短期预判**
- **七 关键反证条件**

## 项目结构

```
auto-news-briefing/
├── rotate_keyword.py         # 权限词轮换
├── validate_report.py        # 格式验证 + 自动修复
├── send_to_subscribers.py    # 订阅群发
├── send_email.py             # 邮件模块 (MD→HTML)
├── watch_email.py            # 即时触发监听
├── check_email.py            # POP3 工具
├── auth_keyword.txt          # 鉴权词
├── subscribers.txt.example   # 订阅列表示例
├── .env.example              # 环境变量模板
└── reports/                  # 简报存档（gitignore）
```

## License

MIT
