"""
邮件收件检查模块 — 通过 POP3 检查收件箱
163 邮箱封锁了 IMAP SELECT（"Unsafe Login"安全策略），改用 POP3 协议
支持检索、搜索、确认日报是否送达
"""
import os
import re
import email
import poplib
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path
# ── .env 加载 ─────────────────────────────────────────────────────────
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    with open(_ENV_FILE, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                _key, _val = _key.strip(), _val.strip().strip('"').strip("'")
                if _key not in os.environ:
                    os.environ[_key] = _val



# ── 配置（优先从环境变量读取）────────────────────────────────────────────
POP_HOST = os.getenv("NEWS_POP_HOST", "pop.163.com")
POP_PORT = int(os.getenv("NEWS_POP_PORT", "995"))

# 监控邮箱（日报发送方，检查收件箱中的回执/退信/回复）
DEFAULT_ACCOUNT = os.getenv("NEWS_EMAIL_ACCOUNT")
DEFAULT_PASSWORD = os.getenv("NEWS_EMAIL_PASSWORD")

if not DEFAULT_ACCOUNT or not DEFAULT_PASSWORD:
    raise RuntimeError("NEWS_EMAIL_ACCOUNT/NEWS_EMAIL_PASSWORD 未设置，请检查 .env 文件")


@dataclass
class EmailInfo:
    """邮件信息结构"""
    index: int            # POP3 序号
    subject: str          # 主题
    sender: str           # 发件人
    receiver: str         # 收件人
    date: datetime        # 发送时间
    size: int             # 大小（字节）
    body_preview: str     # 正文前 300 字
    body_full: str        # 完整正文

    def summary(self) -> str:
        return (
            f"[#{self.index:04d}] "
            f"{self.date.strftime('%m-%d %H:%M')} | "
            f"{self.sender:<30} | "
            f"{self.subject}"
        )


@dataclass
class CheckResult:
    """检查结果"""
    checked_at: datetime
    account: str
    total_count: int
    emails: list = field(default_factory=list)

    @property
    def latest(self) -> Optional[EmailInfo]:
        return self.emails[0] if self.emails else None

    def summary(self) -> str:
        lines = [
            f"[INBOX] {self.account}",
            f"   检查时间: {self.checked_at.strftime('%Y-%m-%d %H:%M:%S')}",
            f"   总邮件数: {self.total_count}",
            f"   匹配结果: {len(self.emails)}",
        ]
        if self.emails:
            lines.append("   " + "─" * 52)
            for em in self.emails[:20]:
                lines.append(f"   {em.summary()}")
        return "\n".join(lines)


# ── 核心函数 ──────────────────────────────────────────────────────────────

def _decode_str(s) -> str:
    """解码邮件头字符串（处理 =?UTF-8?B?...?= 等编码）"""
    if s is None:
        return ""
    parts = decode_header(s)
    result = []
    for payload, charset in parts:
        if isinstance(payload, bytes):
            try:
                result.append(payload.decode(charset or "utf-8", errors="replace"))
            except Exception:
                result.append(payload.decode("utf-8", errors="replace"))
        else:
            result.append(str(payload))
    return "".join(result)


def _parse_email_body(msg) -> tuple:
    """从 email.Message 中提取正文，返回 (plain_text, html_text)"""
    plain_text = ""
    html_text = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disposition:
                continue

            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                decoded = payload.decode("utf-8", errors="replace")

            if content_type == "text/plain" and not plain_text:
                plain_text = decoded
            elif content_type == "text/html" and not html_text:
                html_text = decoded
    else:
        content_type = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                decoded = payload.decode("utf-8", errors="replace")
            if content_type == "text/html":
                html_text = decoded
            else:
                plain_text = decoded

    return plain_text, html_text


def _clean_html(html_text: str) -> str:
    """简单去除 HTML 标签，提取纯文本"""
    text = re.sub(r"<style[^>]*>.*?</style>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</?p[^>]*>", "\n", text)
    text = re.sub(r"</?div[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_raw_email(raw_bytes: bytes, index: int) -> Optional[EmailInfo]:
    """解析原始邮件数据"""
    try:
        msg = email.message_from_bytes(raw_bytes)
    except Exception:
        return None

    subject = _decode_str(msg.get("Subject", "(无主题)"))
    sender_raw = _decode_str(msg.get("From", "未知"))
    sender_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", sender_raw)
    sender = sender_match.group(0) if sender_match else sender_raw
    receiver = _decode_str(msg.get("To", ""))

    date_str = msg.get("Date", "")
    try:
        date = parsedate_to_datetime(date_str)
        # 去掉时区信息，统一为 naive datetime
        if date.tzinfo is not None:
            date = date.replace(tzinfo=None)
    except Exception:
        date = datetime.now()

    plain_text, html_text = _parse_email_body(msg)
    body = plain_text if plain_text else _clean_html(html_text)
    body_preview = body[:300].replace("\n", " ").strip()

    return EmailInfo(
        index=index,
        subject=subject,
        sender=sender,
        receiver=receiver,
        date=date,
        size=len(raw_bytes),
        body_preview=body_preview,
        body_full=body,
    )


def _connect_pop3(account: str, password: str) -> poplib.POP3_SSL:
    """建立 POP3 连接并登录"""
    conn = poplib.POP3_SSL(POP_HOST, POP_PORT, timeout=15)
    conn.user(account)
    conn.pass_(password)
    return conn


# ── 公共 API ───────────────────────────────────────────────────────────────

def check_inbox(
    account: str = None,
    password: str = None,
    days: int = 3,
    max_results: int = 50,
    max_download: int = 200,
    subject_keyword: str = None,
    sender_keyword: str = None,
) -> CheckResult:
    """
    通过 POP3 检查收件箱最近邮件

    POP3 不支持服务端搜索，只能下载邮件头或全文后在本地过滤。
    本函数采用"先下载头部（TOP），匹配后再下载全文"的策略提升速度。

    参数:
        account: 邮箱账号（默认用 DEFAULT_ACCOUNT）
        password: 邮箱密码/授权码
        days: 检查最近 N 天的邮件
        max_results: 最多返回多少封
        max_download: 最多从服务器下载多少封（POP3 无搜索，需逐封检查）
        subject_keyword: 主题关键词过滤（可选）
        sender_keyword: 发件人关键词过滤（可选）

    返回:
        CheckResult 对象
    """
    account = account or DEFAULT_ACCOUNT
    password = password or DEFAULT_PASSWORD

    if not password:
        print(f"[SKIP] 未配置 {account} 的密码，跳过检查")
        return CheckResult(checked_at=datetime.now(), account=account, total_count=0)

    since_dt = datetime.now() - timedelta(days=days)
    conn = _connect_pop3(account, password)

    try:
        total_count = len(conn.list()[1])
        # 只检查最近的 N 封
        start = max(1, total_count - max_download + 1)
        emails = []

        for i in range(total_count, start - 1, -1):
            if len(emails) >= max_results:
                break

            try:
                # 先下载邮件头（TOP N 0 = 只取头部）
                resp, lines, octets = conn.top(i, 0)
                raw_headers = b"\n".join(lines)
                msg_headers = email.message_from_bytes(raw_headers)

                # 解析日期，过滤旧邮件
                date_str = msg_headers.get("Date", "")
                try:
                    mail_date = parsedate_to_datetime(date_str)
                    # 去掉时区信息，转为 naive datetime 以便与 since_dt 比较
                    if mail_date.tzinfo is not None:
                        mail_date = mail_date.replace(tzinfo=None)
                except Exception:
                    mail_date = datetime.now()
                if mail_date < since_dt:
                    continue

                # 解析主题
                subject = _decode_str(msg_headers.get("Subject", ""))

                # 关键词过滤
                if subject_keyword and subject_keyword.lower() not in subject.lower():
                    continue
                if sender_keyword:
                    sender_raw = _decode_str(msg_headers.get("From", ""))
                    if sender_keyword.lower() not in sender_raw.lower():
                        continue

                # 匹配成功 → 下载全文
                resp, lines, octets = conn.retr(i)
                raw_data = b"\n".join(lines)
                em = _parse_raw_email(raw_data, i)
                if em:
                    emails.append(em)

            except Exception as e:
                # 单封邮件解析失败不阻断整体流程
                continue

        return CheckResult(
            checked_at=datetime.now(),
            account=account,
            total_count=total_count,
            emails=emails,
        )
    finally:
        conn.quit()


def search_emails(
    keyword: str,
    account: str = None,
    password: str = None,
    days: int = 7,
    search_body: bool = False,
) -> list:
    """
    按关键词搜索邮件（POP3 本地过滤）

    参数:
        keyword: 搜索关键词
        account: 邮箱账号
        password: 邮箱密码
        days: 搜索最近 N 天
        search_body: 是否搜索正文（默认仅搜索主题，设为 True 会下载全文，速度较慢）

    返回:
        EmailInfo 列表
    """
    account = account or DEFAULT_ACCOUNT
    password = password or DEFAULT_PASSWORD

    if not password:
        return []

    since_dt = datetime.now() - timedelta(days=days)
    conn = _connect_pop3(account, password)

    try:
        total_count = len(conn.list()[1])
        results = []

        for i in range(total_count, 0, -1):
            try:
                if search_body:
                    # 搜索正文需下载全文
                    resp, lines, octets = conn.retr(i)
                    raw_data = b"\n".join(lines)
                    em = _parse_raw_email(raw_data, i)
                    if em and em.date >= since_dt:
                        if (keyword.lower() in em.subject.lower() or
                                keyword.lower() in em.body_full.lower()):
                            results.append(em)
                else:
                    # 仅搜索主题，只需下载头部
                    resp, lines, octets = conn.top(i, 0)
                    raw_headers = b"\n".join(lines)
                    msg_headers = email.message_from_bytes(raw_headers)

                    date_str = msg_headers.get("Date", "")
                    try:
                        mail_date = parsedate_to_datetime(date_str)
                    except Exception:
                        mail_date = datetime.now()
                    if mail_date < since_dt:
                        continue

                    subject = _decode_str(msg_headers.get("Subject", ""))
                    if keyword.lower() in subject.lower():
                        # 匹配 → 下载全文
                        resp, lines, octets = conn.retr(i)
                        raw_data = b"\n".join(lines)
                        em = _parse_raw_email(raw_data, i)
                        if em:
                            results.append(em)

                if len(results) >= 30:
                    break
            except Exception:
                continue

        return results
    finally:
        conn.quit()


def check_daily_report(
    date_str: str = None,
    account: str = None,
    password: str = None,
) -> dict:
    """
    检查指定日期的日报是否已被收件方收到

    参数:
        date_str: 日期字符串 YYYY-MM-DD，默认今天
        account: 收件邮箱（默认用 DEFAULT_ACCOUNT）
        password: 收件邮箱密码

    返回:
        {"found": bool, "email": EmailInfo|None, "message": str}
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    account = account or DEFAULT_ACCOUNT
    password = password or DEFAULT_PASSWORD

    if not password:
        return {
            "found": False,
            "email": None,
            "message": f"[WARN] 未配置 {account} 的密码，无法检查",
        }

    # 搜索主题关键词（匹配日报标题格式）
    keywords = [f"每日新闻简报 | {date_str}", "每日新闻简报"]
    for kw in keywords:
        results = search_emails(
            keyword=kw,
            account=account,
            password=password,
            days=3,
        )
        if results:
            return {
                "found": True,
                "email": results[0],
                "message": f"[OK] 已找到日报邮件：{results[0].subject}",
            }

    return {
        "found": False,
        "email": None,
        "message": f"[FAIL] 未找到 {date_str} 的日报邮件（在 {account} 中）",
    }


def check_latest_report() -> dict:
    """检查最近一次日报是否收到（自动匹配 reports/ 目录中最新的报告日期）"""
    import glob as gb
    reports = sorted(gb.glob("reports/daily-*.md"))
    if not reports:
        return {"found": False, "email": None, "message": "[WARN] 未找到任何日报文件"}

    latest = reports[-1]
    date_match = re.search(r"daily-(\d{4}-\d{2}-\d{2})", latest)
    if not date_match:
        return {"found": False, "email": None, "message": "[WARN] 无法解析报告日期"}

    return check_daily_report(date_str=date_match.group(1))


def check_sent_box(
    days: int = 1,
    account: str = None,
    password: str = None,
) -> CheckResult:
    """
    检查发件箱（Daily Report 发送方），确认邮件已从发件服务器发出

    163 邮箱 POP3 默认返回所有邮件（含已发送？视服务器策略而定）。
    如果 POP3 不支持检查已发送，此函数返回有限结果。
    """
    account = account or DEFAULT_ACCOUNT
    password = password or DEFAULT_PASSWORD

    return check_inbox(
        account=account,
        password=password,
        days=days,
        max_results=20,
        subject_keyword="每日新闻简报",
        max_download=50,
    )


# ── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import argparse

    parser = argparse.ArgumentParser(description="[Email] 邮件收件检查工具 (POP3)")
    sub = parser.add_subparsers(dest="command")

    # check — 检查收件箱
    p_check = sub.add_parser("check", help="检查收件箱最近邮件")
    p_check.add_argument("-d", "--days", type=int, default=3, help="最近 N 天（默认 3）")
    p_check.add_argument("-n", "--max", type=int, default=20, help="最多返回 N 封（默认 20）")
    p_check.add_argument("-a", "--account", default=None, help="邮箱账号")
    p_check.add_argument("-p", "--password", default=None, help="邮箱密码")
    p_check.add_argument("-s", "--subject", default=None, help="主题关键词过滤")

    # search — 搜索
    p_search = sub.add_parser("search", help="搜索邮件")
    p_search.add_argument("keyword", help="搜索关键词")
    p_search.add_argument("-d", "--days", type=int, default=7, help="最近 N 天（默认 7）")
    p_search.add_argument("--body", action="store_true", help="同时搜索正文")

    # report — 检查日报送达（搜索收件箱中文档同步的日报回执）
    p_report = sub.add_parser("report", help="检查日报发送回执")
    p_report.add_argument("date", nargs="?", default=None, help="日期 YYYY-MM-DD（默认今天）")

    # sent — 检查发件箱
    p_sent = sub.add_parser("sent", help="检查日报是否已发出")
    p_sent.add_argument("-d", "--days", type=int, default=1)

    args = parser.parse_args()

    if args.command == "check":
        result = check_inbox(
            account=args.account,
            password=args.password,
            days=args.days,
            max_results=args.max,
            subject_keyword=args.subject,
        )
        print(result.summary())
        if result.emails and result.latest:
            print(f"\n>> 最新邮件: {result.latest.summary()}")

    elif args.command == "search":
        results = search_emails(args.keyword, days=args.days, search_body=args.body)
        print(f"[SEARCH] '{args.keyword}' => {len(results)} 封:")
        for em in results:
            print(f"   {em.summary()}")

    elif args.command == "report":
        if args.date:
            result = check_daily_report(date_str=args.date)
        else:
            result = check_latest_report()
        print(result["message"])
        if result["email"]:
            em = result["email"]
            print(f"   {em.summary()}")
            print(f"   正文预览: {em.body_preview[:150]}...")

    elif args.command == "sent":
        result = check_sent_box(days=args.days)
        print(f"[SENT] {result.account}")
        print(f"   含'每日新闻简报'的邮件: {len(result.emails)} 封")
        for em in result.emails:
            print(f"   {em.summary()}")

    else:
        print("=" * 55)
        print("  check_email.py -- 邮件检查模块 (POP3)")
        print(f"  监控邮箱: {DEFAULT_ACCOUNT}")
        print(f"  POP3:     {POP_HOST}:{POP_PORT}")
        print("=" * 55)
        print("\n用法:")
        print("  python check_email.py check              查看收件箱最近邮件")
        print("  python check_email.py check -d 7          查看最近 7 天")
        print("  python check_email.py check -s '日报'     过滤主题含'日报'的邮件")
        print("  python check_email.py search <关键词>      搜索邮件主题")
        print("  python check_email.py search <词> --body   搜索主题+正文")
        print("  python check_email.py report              检查今日日报回执")
        print("  python check_email.py report 2026-06-27   检查指定日期回执")
        print("  python check_email.py sent                检查日报是否已发出")
        print("\n代码调用:")
        print("  from check_email import check_inbox, search_emails, check_daily_report")
        print("  result = check_inbox(days=3)")
        print("  emails = search_emails('日报', days=7)")
        print("  status = check_daily_report('2026-06-27')")
