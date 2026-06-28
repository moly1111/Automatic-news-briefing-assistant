"""
邮件发送模块 — 支持纯文本和 Markdown/HTML 格式
通过 163 邮箱 SMTP 发送，优先从环境变量读取敏感信息
"""
import os
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
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


# ── 配置（优先从环境变量读取，否则使用默认值）────────────────────────────
SENDER_EMAIL = os.getenv("NEWS_SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("NEWS_SENDER_PASSWORD")
RECEIVER_EMAIL = os.getenv("NEWS_RECEIVER_EMAIL")
SMTP_HOST = os.getenv("NEWS_SMTP_HOST", "smtp.163.com")
SMTP_PORT = int(os.getenv("NEWS_SMTP_PORT", "465"))
if not SENDER_EMAIL or not SENDER_PASSWORD:
    raise RuntimeError("NEWS_SENDER_EMAIL/NEWS_SENDER_PASSWORD 未设置，请检查 .env 文件")


def _simple_md_to_html(md_text: str) -> str:
    """
    轻量级 Markdown → HTML 转换器（无需第三方库）
    覆盖常用的 MD 语法：标题、加粗、列表、链接、段落、分隔线、行内代码
    """
    lines = md_text.split("\n")
    html_lines = []
    in_list = False
    in_ordered_list = False

    for line in lines:
        stripped = line.strip()

        # 水平分隔线
        if re.match(r"^[-*_]{3,}$", stripped):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if in_ordered_list:
                html_lines.append("</ol>")
                in_ordered_list = False
            html_lines.append("<hr>")
            continue

        # 标题 (# ~ ######)
        header_match = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if header_match:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if in_ordered_list:
                html_lines.append("</ol>")
                in_ordered_list = False
            level = len(header_match.group(1))
            content = _inline_md_to_html(header_match.group(2))
            html_lines.append(f"<h{level}>{content}</h{level}>")
            continue

        # 无序列表
        ul_match = re.match(r"^[-*+]\s+(.*)", stripped)
        if ul_match:
            if in_ordered_list:
                html_lines.append("</ol>")
                in_ordered_list = False
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            content = _inline_md_to_html(ul_match.group(1))
            html_lines.append(f"<li>{content}</li>")
            continue

        # 有序列表
        ol_match = re.match(r"^\d+\.\s+(.*)", stripped)
        if ol_match:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if not in_ordered_list:
                html_lines.append("<ol>")
                in_ordered_list = True
            content = _inline_md_to_html(ol_match.group(1))
            html_lines.append(f"<li>{content}</li>")
            continue

        # 空行 → 段落分隔；关闭列表
        if not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if in_ordered_list:
                html_lines.append("</ol>")
                in_ordered_list = False
            continue

        # 普通段落
        if in_list:
            html_lines.append("</ul>")
            in_list = False
        if in_ordered_list:
            html_lines.append("</ol>")
            in_ordered_list = False
        content = _inline_md_to_html(stripped)
        html_lines.append(f"<p>{content}</p>")

    # 关闭未闭合的列表
    if in_list:
        html_lines.append("</ul>")
    if in_ordered_list:
        html_lines.append("</ol>")

    return "\n".join(html_lines)


def _inline_md_to_html(text: str) -> str:
    """处理行内 MD 语法：**加粗**、*斜体*、`代码`、[链接](url)、~~删除线~~"""
    # 图片 [alt](url)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<img src="\2" alt="\1">', text)
    # 链接 [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    # 加粗 **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # 斜体 *text*（不冲突已处理的 **）
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", text)
    # 删除线 ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)
    # 行内代码 `code`
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def md_to_html(md_text: str, title: str = "") -> str:
    """
    将 Markdown 文本转为完整 HTML 邮件模板
    优先使用第三方 markdown 库，不可用时回退到内置转换器
    """
    try:
        import markdown
        body_html = markdown.markdown(
            md_text,
            extensions=["extra", "codehilite", "tables", "fenced_code"]
        )
    except ImportError:
        body_html = _simple_md_to_html(md_text)

    # 包裹在邮件友好的 HTML 模板中
    html_template = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{
    max-width: 720px;
    margin: 0 auto;
    padding: 20px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Microsoft YaHei", "Helvetica Neue", sans-serif;
    font-size: 15px;
    line-height: 1.75;
    color: #1a1a1a;
    background: #fafafa;
  }}
  .container {{
    background: #ffffff;
    border-radius: 8px;
    padding: 32px 40px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  }}
  h1 {{ font-size: 1.6em; border-bottom: 2px solid #e8e8e8; padding-bottom: 10px; margin-top: 0; }}
  h2 {{ font-size: 1.3em; margin-top: 28px; color: #2c3e50; }}
  h3 {{ font-size: 1.1em; margin-top: 22px; color: #34495e; }}
  h4 {{ font-size: 1.0em; margin-top: 18px; color: #555; }}
  hr {{ border: none; border-top: 1px solid #eee; margin: 24px 0; }}
  ul, ol {{ padding-left: 24px; }}
  li {{ margin: 6px 0; }}
  a {{ color: #1890ff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{ background: #f5f5f5; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
  pre {{ background: #f5f5f5; padding: 12px 16px; border-radius: 6px; overflow-x: auto; }}
  blockquote {{
    border-left: 4px solid #1890ff;
    margin: 16px 0;
    padding: 4px 16px;
    color: #555;
    background: #f0f7ff;
    border-radius: 0 4px 4px 0;
  }}
  strong {{ color: #1a1a1a; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
  th, td {{ border: 1px solid #e8e8e8; padding: 8px 12px; text-align: left; }}
  th {{ background: #f5f5f5; }}
  .footer {{
    margin-top: 24px;
    padding-top: 16px;
    border-top: 1px solid #eee;
    color: #999;
    font-size: 0.85em;
    text-align: center;
  }}
</style>
</head>
<body>
<div class="container">
{body_html}
</div>
<div class="footer">
  <p>📮 本报告由 AI 自动生成 | 如有疑问请直接回复</p>
</div>
</body>
</html>"""
    return html_template


def send_email(subject: str, message_body: str,
               receiver_email: str = None,
               sender_email: str = None,
               sender_password: str = None):
    """
    发送纯文本邮件（保持向后兼容）
    """
    receiver_email = receiver_email or RECEIVER_EMAIL
    sender_email = sender_email or SENDER_EMAIL
    sender_password = sender_password or SENDER_PASSWORD

    msg = MIMEText(message_body, "plain", "utf-8")
    msg["From"] = Header(sender_email)
    msg["To"] = Header(receiver_email)
    msg["Subject"] = Header(subject, "utf-8")

    try:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT)
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, receiver_email, msg.as_string())
        print("[OK] 邮件发送成功")
    except Exception as e:
        print("[FAIL] 邮件发送失败:", e)
        raise
    finally:
        server.quit()


def send_md_email(subject: str, md_content: str,
                  receiver_email: str = None,
                  sender_email: str = None,
                  sender_password: str = None):
    """
    发送 Markdown 格式邮件（转为 HTML 发送，同时附带纯文本版本）

    参数:
        subject: 邮件主题
        md_content: Markdown 格式的邮件正文
        receiver_email: 收件人（默认从环境变量读取）
        sender_email: 发件人（默认从环境变量读取）
        sender_password: 授权码（默认从环境变量读取）
    """
    receiver_email = receiver_email or RECEIVER_EMAIL
    sender_email = sender_email or SENDER_EMAIL
    sender_password = sender_password or SENDER_PASSWORD

    # 构建 HTML 版本
    html_content = md_to_html(md_content, title=subject)

    # 构建 multipart 邮件（同时包含纯文本和 HTML）
    msg = MIMEMultipart("alternative")
    msg["From"] = Header(sender_email)
    msg["To"] = Header(receiver_email)
    msg["Subject"] = Header(subject, "utf-8")

    # 纯文本部分（去除 MD 标记的纯文本）
    plain_text = re.sub(r"[#*`>\[\]()!~_-]", "", md_content)
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    # HTML 部分
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT)
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, receiver_email, msg.as_string())
        print("[OK] Markdown 邮件发送成功")
    except Exception as e:
        print("[FAIL] 邮件发送失败:", e)
        raise
    finally:
        server.quit()


# ── CLI 快速测试 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  send_email.py — 邮件模块就绪")
    print(f"  发件人: {SENDER_EMAIL}")
    print(f"  收件人: {RECEIVER_EMAIL}")
    print(f"  SMTP:   {SMTP_HOST}:{SMTP_PORT}")
    print("=" * 50)
    print("\n用法:")
    print("  from send_email import send_md_email, send_email")
    print('  send_md_email("主题", "# 标题\\n\\n正文内容")')
