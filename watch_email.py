"""
邮件监听触发器 — 常驻进程，持续监控收件箱
当收到匹配鉴权关键词的触发邮件时，自动回复最新日报

机制：
  - 轮询 POP3 收件箱（默认每 30 秒）
  - 检测新邮件：主题 == auth_subject AND 正文 == auth_body
  - 两个条件必须同时精确匹配
  - auth_keyword.txt 每次轮询热加载，支持运行时修改
  - 匹配后自动发送最新日报到发件人邮箱
"""
import os
import sys
import time
import json
import signal
from datetime import datetime, timedelta
from pathlib import Path

# 确保能 import 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from send_email import send_md_email
from check_email import (
    _connect_pop3,
    _decode_str,
    _parse_email_body,
    _clean_html,
    DEFAULT_ACCOUNT,
    DEFAULT_PASSWORD,
)

# ── 配置 ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
AUTH_FILE = SCRIPT_DIR / "auth_keyword.txt"
REPORTS_DIR = SCRIPT_DIR / "reports"
STATE_FILE = SCRIPT_DIR / ".watch_state.json"

# 默认值（被 auth_keyword.txt 覆盖）
DEFAULT_AUTH_SUBJECT = "banana"
DEFAULT_AUTH_BODY = "最新新闻简报"

# 轮询配置
POLL_INTERVAL = int(os.getenv("WATCH_POLL_INTERVAL", "30"))  # 秒
MAX_RETRIES = int(os.getenv("WATCH_MAX_RETRIES", "3"))

# 运行标志
_running = True


# ── 鉴权关键词热加载 ────────────────────────────────────────────────────

def load_auth_keywords():
    """
    从 auth_keyword.txt 热加载鉴权关键词
    文件格式：第一行 = 主题关键词，第二行 = 正文关键词
    支持运行时修改，每次轮询自动重读
    """
    if not AUTH_FILE.exists():
        print(f"[WARN] {AUTH_FILE} 不存在，使用默认值")
        return DEFAULT_AUTH_SUBJECT, DEFAULT_AUTH_BODY

    try:
        lines = []
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:  # 跳过空行
                    lines.append(stripped)

        subject = lines[0] if len(lines) >= 1 else DEFAULT_AUTH_SUBJECT
        body = lines[1] if len(lines) >= 2 else DEFAULT_AUTH_BODY
        return subject, body
    except Exception as e:
        print(f"[WARN] 读取 {AUTH_FILE} 失败: {e}，使用默认值")
        return DEFAULT_AUTH_SUBJECT, DEFAULT_AUTH_BODY


# ── 邮件指纹 ────────────────────────────────────────────────────────────

def _make_fingerprint(email_info: dict) -> str:
    """
    生成邮件唯一指纹（不依赖 POP3 序号，序号可能因删邮件而移位）
    使用 date + sender + subject 的哈希，足够区分不同邮件
    """
    import hashlib
    raw = f"{email_info.get('date','')}|{email_info.get('sender','')}|{email_info.get('subject','')}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


# ── 状态持久化 ──────────────────────────────────────────────────────────

def load_state():
    """
    加载状态
    返回: {"last_seen_index": int, "triggered": set, "last_run": str|None}
    """
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            data["triggered"] = set(data.get("triggered", []))
            return data
        except Exception:
            pass
    return {"last_seen_index": 0, "triggered": set(), "last_run": None}


def save_state(state: dict):
    """持久化状态（set → list for JSON）"""
    out = dict(state)
    out["triggered"] = sorted(list(state.get("triggered", set())))
    out["last_run"] = datetime.now().isoformat()
    # 限制指纹历史最多保留 500 条
    if len(out["triggered"]) > 500:
        out["triggered"] = out["triggered"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(out, f, indent=2)


def mark_triggered(state: dict, fingerprint: str, index: int):
    """标记邮件已触发，立即持久化（防止崩溃/重启重复发送）"""
    state.setdefault("triggered", set()).add(fingerprint)
    state["last_seen_index"] = max(state.get("last_seen_index", 0), index)
    save_state(state)


# ── 邮件扫描 ────────────────────────────────────────────────────────────

def scan_new_emails(account: str, password: str, last_seen_index: int):
    """
    扫描收件箱中序号大于 last_seen_index 的新邮件
    返回 (new_emails, latest_index)
    """
    import email as em_module
    from email.utils import parsedate_to_datetime

    conn = _connect_pop3(account, password)
    new_emails = []
    latest_index = last_seen_index

    try:
        total_count = len(conn.list()[1])

        for i in range(last_seen_index + 1, total_count + 1):
            try:
                resp, lines, octets = conn.retr(i)
                raw_data = b"\n".join(lines)
                msg = em_module.message_from_bytes(raw_data)

                # 解析头部
                subject = _decode_str(msg.get("Subject", ""))
                sender_raw = _decode_str(msg.get("From", ""))
                import re
                sender_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", sender_raw)
                sender = sender_match.group(0) if sender_match else sender_raw

                date_str = msg.get("Date", "")
                try:
                    mail_date = parsedate_to_datetime(date_str)
                except Exception:
                    mail_date = datetime.now()

                # 解析正文
                plain_text, html_text = _parse_email_body(msg)
                body = plain_text if plain_text else _clean_html(html_text)
                body_stripped = body.strip()

                em = {
                    "index": i,
                    "subject": subject,
                    "sender": sender,
                    "date": mail_date.isoformat() if hasattr(mail_date, "isoformat") else str(mail_date),
                    "body": body_stripped,
                    "body_full": body,
                }
                em["fingerprint"] = _make_fingerprint(em)
                new_emails.append(em)

                latest_index = max(latest_index, i)

            except Exception as e:
                print(f"  [WARN] 解析 #{i} 失败: {e}")
                continue

        return new_emails, latest_index
    finally:
        conn.quit()


# ── 触发处理 ────────────────────────────────────────────────────────────

def handle_trigger(email_info: dict):
    """
    处理匹配的触发邮件：发送最新日报到发件人
    """
    sender = email_info["sender"]
    print(f"\n{'='*55}")
    print(f"[TRIGGER] 收到有效触发请求")
    print(f"  发件人: {sender}")
    print(f"  主题:   {email_info['subject']}")
    print(f"  时间:   {email_info['date']}")
    print(f"{'='*55}")

    # 查找最新日报
    reports = sorted(REPORTS_DIR.glob("daily-*.md"))
    if not reports:
        print("[FAIL] 没有找到任何日报文件，无法回复")
        return False

    latest_report = reports[-1]
    print(f"[INFO] 最新日报: {latest_report.name}")

    # 读取日报内容
    try:
        with open(latest_report, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"[FAIL] 读取日报失败: {e}")
        return False

    # 发送到触发者邮箱
    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"Re: 📰 每日新闻简报 | {today}"

    try:
        send_md_email(
            subject=subject,
            md_content=content,
            receiver_email=sender,
        )
        print(f"[OK] 日报已发送至 {sender}")
        return True
    except Exception as e:
        print(f"[FAIL] 发送失败: {e}")
        return False


# ── 主循环 ──────────────────────────────────────────────────────────────

def watch_loop(account: str = None, password: str = None):
    """
    主监听循环

    参数:
        account: 监控邮箱（从 NEWS_EMAIL_ACCOUNT 环境变量读取）
        password: 邮箱密码
    """
    global _running

    account = account or DEFAULT_ACCOUNT
    password = password or DEFAULT_PASSWORD

    state = load_state()
    last_seen = state.get("last_seen_index", 0)

    print(f"\n{'='*55}")
    print(f"  Email Watchdog 启动")
    print(f"  监控邮箱:   {account}")
    print(f"  鉴权文件:   {AUTH_FILE}")
    print(f"  轮询间隔:   {POLL_INTERVAL}s")
    print(f"  已处理序号: {last_seen}")
    print(f"{'='*55}\n")

    retry_count = 0

    while _running:
        try:
            # 热加载鉴权关键词
            auth_subject, auth_body = load_auth_keywords()
            now = datetime.now().strftime("%H:%M:%S")

            # 扫描新邮件
            new_emails, new_last_seen = scan_new_emails(account, password, last_seen)

            if new_emails:
                print(f"[{now}] 发现 {len(new_emails)} 封新邮件")
                for em in new_emails:
                    fp = em.get("fingerprint", "")
                    print(f"  #{em['index']} | {em['sender']} | \"{em['subject'][:50]}\" | fp={fp}")

                    # 指纹去重：已触发过的邮件绝不再次处理
                    if fp and fp in state.get("triggered", set()):
                        print(f"  >>> 已处理过（指纹 {fp}），跳过")
                        last_seen = max(last_seen, em["index"])
                        continue

                    # 精确匹配鉴权条件
                    subject_match = (em["subject"].strip() == auth_subject.strip())
                    body_match = (em["body"].strip() == auth_body.strip())

                    if subject_match and body_match:
                        print(f"  >>> 匹配成功！触发日报回复...")
                        success = handle_trigger(em)
                        # 无论发送成功与否都标记已触发，避免死循环
                        mark_triggered(state, fp, em["index"])
                        if success:
                            print(f"  >>> [OK] 已标记触发，指纹 {fp}")
                    elif subject_match and not body_match:
                        print(f"  >>> 主题匹配但正文不匹配 (期望: \"{auth_body}\", 实际: \"{em['body'][:80]}\")")
                    elif body_match and not subject_match:
                        print(f"  >>> 正文匹配但主题不匹配 (期望: \"{auth_subject}\", 实际: \"{em['subject']}\")")

                    # 更新序号（即使不触发也要记录已扫描）
                    last_seen = max(last_seen, em["index"])

                # 批次结束后统一持久化序号
                state["last_seen_index"] = last_seen
                save_state(state)

            retry_count = 0  # 重置重试计数

        except Exception as e:
            retry_count += 1
            print(f"[{now}] [ERR] 扫描出错 ({retry_count}/{MAX_RETRIES}): {e}")
            if retry_count >= MAX_RETRIES:
                print("[FATAL] 重试次数用尽，退出")
                break
            _interruptible_sleep(POLL_INTERVAL * 2)  # 出错后等久一点
            continue

        # 等待下一次轮询
        _interruptible_sleep(POLL_INTERVAL)

    print("\n[STOP] Email Watchdog 已停止")


def _interruptible_sleep(seconds: int):
    """可中断的 sleep——每秒醒来检查 _running 标志"""
    for _ in range(seconds):
        if not _running:
            return
        time.sleep(1)


# ── 信号处理 ────────────────────────────────────────────────────────────

_signal_graceful_exit = False

def _signal_handler(signum, frame):
    global _running, _signal_graceful_exit
    if not _signal_graceful_exit:
        _signal_graceful_exit = True
        print(f"\n[STOP] 收到退出信号，正在退出... (再按一次 Ctrl+C 强制退出)")
        _running = False
    else:
        print(f"\n[STOP] 强制退出!")
        import os as _os
        _os._exit(1)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ── CLI ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import argparse

    parser = argparse.ArgumentParser(description="Email Watchdog — 邮件触发日报服务")
    parser.add_argument("-i", "--interval", type=int, default=POLL_INTERVAL,
                        help=f"轮询间隔/秒（默认 {POLL_INTERVAL}）")
    parser.add_argument("-a", "--account", default=None, help="监控邮箱")
    parser.add_argument("-p", "--password", default=None, help="邮箱密码")
    parser.add_argument("--once", action="store_true", help="仅扫描一次，不进入循环")
    parser.add_argument("--reset", action="store_true", help="重置已处理序号")

    args = parser.parse_args()

    # 覆盖轮询间隔
    POLL_INTERVAL = args.interval

    # 重置序号（但保留已触发指纹，防止重复发送）
    if args.reset:
        state = load_state()
        state["last_seen_index"] = 0
        save_state(state)
        print(f"[RESET] 序号已重置（已触发指纹 {len(state.get('triggered', set()))} 条保留）")

    # 单次扫描模式
    if args.once:
        account = args.account or DEFAULT_ACCOUNT
        password = args.password or DEFAULT_PASSWORD
        state = load_state()
        auth_subject, auth_body = load_auth_keywords()
        triggered = state.get("triggered", set())

        print(f"Auth: subject=\"{auth_subject}\", body=\"{auth_body}\"")
        print(f"已触发指纹: {len(triggered)} 条")
        new_emails, latest = scan_new_emails(
            account, password, state.get("last_seen_index", 0)
        )
        print(f"新邮件: {len(new_emails)} 封")
        for em in new_emails:
            fp = em.get("fingerprint", "")
            subj_ok = em["subject"].strip() == auth_subject.strip()
            body_ok = em["body"].strip() == auth_body.strip()
            already_triggered = fp in triggered
            print(f"  #{em['index']} fp={fp} subj=\"{em['subject'][:40]}\" body=\"{em['body'][:40]}\"")
            print(f"       subj_match={subj_ok}, body_match={body_ok}, already_triggered={already_triggered}")
            if subj_ok and body_ok:
                if already_triggered:
                    print(f"       >>> 已触发过，跳过（指纹 {fp}）")
                else:
                    print(f"       >>> MATCH（--once 模式不实际发送，仅标记）")
                    # 标记为已触发，即使 --once 不发送也记住，防止正式模式重复发送
                    mark_triggered(state, fp, em["index"])
        if new_emails:
            # --once 也保存状态，防止下次重启重复扫描
            state["last_seen_index"] = latest
            save_state(state)
            print(f"\n最新序号: {latest} (状态已保存)")
    else:
        # 持续监听
        watch_loop(account=args.account, password=args.password)
