"""
订阅模式 — 每天早上 9:00 向订阅列表群发最新日报
由 cron / 计划任务触发，非持续运行
"""
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SUBSCRIBERS_FILE = SCRIPT_DIR / "subscribers.txt"
REPORTS_DIR = SCRIPT_DIR / "reports"

sys.path.insert(0, str(SCRIPT_DIR))
from send_email import send_md_email


def load_subscribers():
    """加载订阅列表（跳过空行和注释行）"""
    if not SUBSCRIBERS_FILE.exists():
        print(f"[WARN] 订阅文件不存在: {SUBSCRIBERS_FILE}")
        return []

    subscribers = []
    with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                subscribers.append(line)
    return subscribers


def get_latest_report():
    """获取最新日报文件路径"""
    reports = sorted(REPORTS_DIR.glob("daily-*.md"))
    return reports[-1] if reports else None


def send_to_all(date_str: str = None, dry_run: bool = False):
    """
    向所有订阅者发送最新日报

    参数:
        date_str: 日期 YYYY-MM-DD，默认今天
        dry_run: 仅预览，不实际发送
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    subscribers = load_subscribers()
    if not subscribers:
        print("[FAIL] 订阅列表为空，无发送目标")
        return False

    report = get_latest_report()
    if not report:
        print("[FAIL] 没有可发送的日报文件")
        return False

    print(f"订阅模式 — 群发日报")
    print(f"  日期: {date_str}")
    print(f"  报告: {report.name}")
    print(f"  订阅者: {len(subscribers)} 人")
    print(f"  模式: {'DRY RUN（预览）' if dry_run else '正式发送'}")
    print()

    # 读取日报内容
    with open(report, "r", encoding="utf-8") as f:
        content = f.read()

    success_count = 0
    fail_count = 0

    for email_addr in subscribers:
        subject = f"📰 每日新闻简报 | {date_str}"
        print(f"  → {email_addr} ... ", end="")

        if dry_run:
            print("[DRY RUN] 跳过")
            success_count += 1
            continue

        try:
            send_md_email(
                subject=subject,
                md_content=content,
                receiver_email=email_addr,
            )
            print("[OK]")
            success_count += 1
        except Exception as e:
            print(f"[FAIL] {e}")
            fail_count += 1

    print()
    print(f"结果: 成功 {success_count}, 失败 {fail_count}")
    return fail_count == 0


# ── CLI ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="订阅模式 — 向订阅列表群发日报")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际发送")
    parser.add_argument("--date", default=None, help="日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--list", action="store_true", help="仅列出订阅者")

    args = parser.parse_args()

    if args.list:
        subs = load_subscribers()
        print(f"订阅者 ({len(subs)} 人):")
        for s in subs:
            print(f"  {s}")
    else:
        send_to_all(date_str=args.date, dry_run=args.dry_run)
