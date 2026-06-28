"""
Markdown 简报格式验证器 — 在保存为当日简报前校验格式

验证规则:
  1. 表格行前必须有空行或标题
  2. 列表项前必须有空行或标题
  3. fenced code block 成对闭合
  4. 所有表格可渲染为 <table>
  5. 所有列表可渲染为 <li>

所有规则均支持自动修复。规则 1-3 由脚本直接修复，规则 4-5 在 1-3 修复后
重新验证，若仍失败则进入 Claude 工作流处理。

用法:
  python3 validate_report.py <file>             # 仅检查
  python3 validate_report.py <file> --repair    # 检查 + 全部自动修复
  python3 validate_report.py <file> --claude    # 输出 Claude 可用的诊断信息
"""
import re
import sys
from pathlib import Path
from typing import Tuple, List, Dict


# ═══════════════════════════════════════════════════════════════════════════
# 规则 1+2: 扫描缺少空行的表格/列表
# ═══════════════════════════════════════════════════════════════════════════

def _find_missing_blank_lines(md_text: str) -> List[dict]:
    """扫描所有表格和列表行，检查其前一行是否需要空行"""
    issues = []
    lines = md_text.split('\n')

    # 预先找出所有 fenced code block 内的行号，跳过检查
    in_fence = set()
    fence_stack = []
    for i, line in enumerate(lines):
        if re.match(r'^```', line.strip()):
            if fence_stack:
                start = fence_stack.pop()
                for j in range(start, i + 1):
                    in_fence.add(j)
            else:
                fence_stack.append(i)
    for start in fence_stack:
        for j in range(start, len(lines)):
            in_fence.add(j)

    for i, line in enumerate(lines):
        if i in in_fence:
            continue
        stripped = line.strip()
        if not stripped:
            continue

        is_table_row = bool(re.match(r'^\|.+\|', stripped))
        is_sep_row = bool(re.match(r'^\|[\s\-:]+\|', stripped))
        is_list_item = bool(re.match(r'^[-*+]\s+', stripped))

        if not (is_table_row or is_list_item):
            continue

        prev_idx = i - 1
        while prev_idx >= 0 and lines[prev_idx].strip() == '':
            prev_idx -= 1
        if prev_idx < 0:
            continue

        prev_stripped = lines[prev_idx].strip()

        if is_sep_row:
            continue

        # 前一行是标题 / 同一结构 / 分隔线 / 引用 → OK
        if re.match(r'^#{1,6}\s', prev_stripped):
            continue
        if is_table_row and (prev_stripped.startswith('|') or re.match(r'^\|[\s\-:]+\|', prev_stripped)):
            continue
        if is_list_item and re.match(r'^[-*+]\s+', prev_stripped):
            continue
        if re.match(r'^[-*_]{3,}$', prev_stripped):
            continue
        if prev_stripped.startswith('>'):
            continue

        has_blank = any(lines[j].strip() == '' for j in range(prev_idx + 1, i))
        if not has_blank:
            issues.append({
                'line': i + 1,
                'prev_line': prev_idx + 1,
                'type': 'table' if is_table_row else 'list',
                'content': stripped[:60],
                'prev_content': prev_stripped[:60],
            })

    return issues


# ═══════════════════════════════════════════════════════════════════════════
# 规则 3: fenced code block 闭合检查
# ═══════════════════════════════════════════════════════════════════════════

def _find_unclosed_fences(md_text: str) -> List[int]:
    """检查 ``` 是否成对，返回未闭合的 fence 起始行号"""
    lines = md_text.split('\n')
    stack = []
    for i, line in enumerate(lines, 1):
        if re.match(r'^```', line.strip()):
            if stack:
                stack.pop()
            else:
                stack.append(i)
    return stack


# ═══════════════════════════════════════════════════════════════════════════
# 规则 4+5: 渲染验证
# ═══════════════════════════════════════════════════════════════════════════

def _verify_rendering(md_text: str, verbose: bool = False) -> Tuple[List[str], str]:
    """用 python-markdown 渲染，验证表格和列表是否正常生成。
    返回 (问题列表, html)"""
    issues = []
    html = ""
    try:
        import markdown
        html = markdown.markdown(md_text, extensions=['extra', 'tables', 'fenced_code'])

        # 统计非 fenced block 内的表格分隔行
        in_fence = False
        expected_tables = 0
        for line in md_text.split('\n'):
            s = line.strip()
            if s.startswith('```'):
                in_fence = not in_fence
                continue
            if not in_fence and re.match(r'^\|[\s\-:]+\|', s):
                expected_tables += 1

        html_tables = len(re.findall(r'<table>', html))
        if html_tables < expected_tables:
            issues.append(
                f"表格渲染异常：源文件有 {expected_tables} 个表格，但只渲染出 {html_tables} 个 <table>"
            )

        # 列表
        in_fence = False
        md_list_count = 0
        for line in md_text.split('\n'):
            s = line.strip()
            if s.startswith('```'):
                in_fence = not in_fence
                continue
            if not in_fence and re.match(r'^[-*+]\s+', s) and not re.match(r'^[-*_]{3,}$', s):
                md_list_count += 1

        html_li = len(re.findall(r'<li>', html))
        if html_li < md_list_count:
            issues.append(
                f"列表渲染异常：源文件有 {md_list_count} 个列表项，但只渲染出 {html_li} 个 <li>"
            )

    except ImportError:
        issues.append("markdown 库未安装，无法进行渲染验证")
    except Exception as e:
        issues.append(f"渲染验证异常: {e}")

    return issues, html


# ═══════════════════════════════════════════════════════════════════════════
# 自动修复
# ═══════════════════════════════════════════════════════════════════════════

def _repair_blank_lines(md_text: str) -> str:
    """修复规则 1+2：在表格/列表前插入空行"""
    issues = _find_missing_blank_lines(md_text)
    if not issues:
        return md_text

    lines = md_text.split('\n')
    fixed_line_nums = sorted(set(i['line'] for i in issues), reverse=True)
    for line_num in fixed_line_nums:
        lines.insert(line_num - 1, '')
    return '\n'.join(lines)


def _repair_fences(md_text: str) -> str:
    """修复规则 3：闭合未关闭的 fenced code block"""
    unclosed = _find_unclosed_fences(md_text)
    if not unclosed:
        return md_text
    # 在文件末尾补上对应数量的 ```
    return md_text.rstrip('\n') + '\n' + '\n```\n' * len(unclosed)


def auto_repair(md_text: str) -> Tuple[str, Dict]:
    """
    自动修复规则 1-3。
    返回 (修复后文本, {修复统计})
    """
    # 统计修复前的空行问题数
    blank_issues_before = len(_find_missing_blank_lines(md_text))
    fixed = _repair_blank_lines(md_text)

    # 统计修复前的 fence 问题数
    fences_before = len(_find_unclosed_fences(fixed))
    fixed = _repair_fences(fixed)

    return fixed, {
        'blank_lines': blank_issues_before,
        'fences': fences_before,
    }

# ═══════════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════════

def validate(md_text: str) -> Tuple[bool, List[str]]:
    """验证 MD 文本格式。返回 (通过, 问题列表)"""
    all_issues = []

    blank_issues = _find_missing_blank_lines(md_text)
    for issue in blank_issues:
        all_issues.append(
            f"第{issue['line']}行: {issue['type']}前缺空行 ← "
            f"第{issue['prev_line']}行 \"{issue['prev_content']}\""
        )

    unclosed = _find_unclosed_fences(md_text)
    for line_num in unclosed:
        all_issues.append(f"第{line_num}行: fenced code block (```) 未闭合")

    render_issues, _ = _verify_rendering(md_text)
    all_issues.extend(render_issues)

    return len(all_issues) == 0, all_issues


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Markdown 简报格式验证器')
    parser.add_argument('file', help='MD 文件路径')
    parser.add_argument('--repair', '-r', action='store_true',
                        help='自动修复全部可修复问题（规则 1-3）')
    parser.add_argument('--claude', '-c', action='store_true',
                        help='输出 Claude 诊断上下文：原始内容 + 渲染问题详情')
    parser.add_argument('--quiet', '-q', action='store_true',
                        help='仅输出错误')

    args = parser.parse_args()

    filepath = Path(args.file)
    if not filepath.exists():
        print(f'[FAIL] 文件不存在: {args.file}')
        sys.exit(1)

    content = filepath.read_text(encoding='utf-8')

    # ── Claude 诊断模式 ──
    if args.claude:
        passed, issues = validate(content)
        if passed:
            print('✅ 格式检查全部通过，无需 Claude 干预。')
            sys.exit(0)

        print('# Claude 诊断任务\n')
        print('## 问题列表\n')
        for issue in issues:
            print(f'- {issue}')
        print()

        # 哪些问题可以脚本修复
        can_auto = [i for i in issues if '缺空行' in i or 'fenced' in i]
        need_claude = [i for i in issues if i not in can_auto]

        if can_auto:
            fixed, stats = auto_repair(content)
            passed2, issues2 = validate(fixed)
            if passed2:
                print(f'## 脚本已自动修复 {len(can_auto)} 个问题，现在全部通过。')
                print('无需 Claude 干预。')
                # 保存修复结果
                filepath.write_text(fixed, encoding='utf-8')
                print(f'💾 已保存修复后的文件: {filepath}')
                sys.exit(0)
            else:
                print(f'## 脚本已修复 {len(can_auto)} 个问题，但有 {len(issues2)} 个仍需处理')
                content = fixed
                issues = issues2

        if need_claude:
            print('## 需要 Claude 处理的问题\n')
            render_issues, html = _verify_rendering(content, verbose=True)
            for ri in render_issues:
                print(f'- {ri}')
            print()

            # 找出渲染失败的具体段落
            lines = content.split('\n')
            print('## 源文件（含行号）\n')
            in_fence = False
            for i, line in enumerate(lines, 1):
                s = line.strip()
                if s.startswith('```'):
                    in_fence = not in_fence
                marker = ''
                if not in_fence:
                    if re.match(r'^\|.+\|', s) or re.match(r'^[-*+]\s+', s):
                        # 检查前一行是否空行
                        prev = lines[i-2].strip() if i >= 2 else ''
                        if prev and not prev.startswith('|') and not prev.startswith('-') and not prev.startswith('#') and not re.match(r'^\|[\s\-:]+\|', prev):
                            marker = '  ← ⚠️ 前一行非空/非标题/非表格'
                print(f'{i:4d} {marker}  {line}')
        sys.exit(0)

    # ── 普通检查模式 ──
    passed, issues = validate(content)

    if not args.quiet:
        print(f'📋 检查文件: {filepath.name}')
        blank_count = sum(1 for i in issues if '缺空行' in i)
        fence_count = sum(1 for i in issues if 'fenced' in i)
        render_count = sum(1 for i in issues if '渲染' in i)

        b = '✅ 通过' if blank_count == 0 else f'❌ {blank_count} 个问题'
        f = '✅ 通过' if fence_count == 0 else f'❌ {fence_count} 个问题'
        r = '✅ 通过' if render_count == 0 else f'❌ {render_count} 个问题'
        print(f'   规则 1+2 (空行): {b}')
        print(f'   规则 3 (代码块): {f}')
        print(f'   规则 4+5 (渲染): {r}')

        if issues:
            print(f'\n--- 问题详情 ---')
            for issue in issues:
                print(f'  • {issue}')

    if passed:
        if not args.quiet:
            print(f'\n✅ 全部检查通过')
        sys.exit(0)

    # ── 修复模式 ──
    if args.repair:
        print(f'\n🔧 自动修复（规则 1-3）...')
        fixed, stats = auto_repair(content)
        print(f'   空行修复: {stats["blank_lines"]} 行变更')
        print(f'   代码块闭合: {stats["fences"]} 行变更')

        passed2, issues2 = validate(fixed)
        if passed2:
            filepath.write_text(fixed, encoding='utf-8')
            print(f'   ✅ 修复后全部验证通过')
            print(f'   💾 已保存修复后的文件')
            sys.exit(0)
        else:
            print(f'   ⚠️  修复后仍有 {len(issues2)} 个问题，需 Claude 介入:')
            for i in issues2:
                print(f'      • {i}')
            # 即使有剩余问题，也保存（规则 1-3 的修复是安全的）
            filepath.write_text(fixed, encoding='utf-8')
            print(f'   💾 已保存（规则 1-3 已修复，剩余问题待 Claude 处理）')
            print(f'\n💡 运行 --claude 获取诊断上下文')
            sys.exit(1)
    else:
        print(f'\n[FAIL] 格式检查不通过。')
        print(f'  python3 validate_report.py {args.file} --repair   # 自动修复')
        print(f'  python3 validate_report.py {args.file} --claude   # Claude 诊断')
        sys.exit(1)
