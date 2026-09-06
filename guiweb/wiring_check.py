"""wiring_check.py — guiweb 前端接线静态检查（零网络、纯文本分析）。

验收目标：契约里的每个方法在 mock.js 有实现、在 app.js 被调用（或列入白名单）；
app.js 引用的每个元素 id 在 index.html 存在；无重复 id；无任何外链（离线铁律）。

返回问题字符串列表（空 = 全绿）。tests/test_guiweb.py 会调用本检查。
"""
import re
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent / "ui"
ROOT = Path(__file__).resolve().parent.parent

# 允许未被 app.js 直接调用的契约方法（例如由生成的设置页经统一入口调用）
ALLOW_UNCALLED = set()


def _contract_methods():
    """从 contracts.md 提取 `### method(` 形式的方法名。"""
    md = (Path(__file__).resolve().parent / "contracts.md").read_text(encoding="utf-8")
    return set(re.findall(r"^### ([a-z_]+)\(", md, re.M))


def run_checks():
    problems = []
    html_p = UI_DIR / "index.html"
    if not html_p.exists():
        return ["guiweb/ui/index.html 不存在"]
    html = html_p.read_text(encoding="utf-8")
    js_files = {}
    for name in ("app.js", "mock.js"):
        p = UI_DIR / name
        if not p.exists():
            problems.append("缺少 %s" % name)
            js_files[name] = ""
        else:
            js_files[name] = p.read_text(encoding="utf-8")
    app_js = js_files.get("app.js", "")
    mock_js = js_files.get("mock.js", "")
    css_p = UI_DIR / "app.css"
    if not css_p.exists():
        problems.append("缺少 app.css")

    # ---- 离线铁律：不允许任何外链（localhost/127.0.0.1 仅为配置值，放行）----
    for label, text in (("index.html", html), ("app.js", app_js), ("mock.js", mock_js),
                        ("app.css", css_p.read_text(encoding="utf-8") if css_p.exists() else "")):
        for m in re.finditer(r'https?://[^\s"\'<>)]+', text):
            url = m.group(0)
            if "www.w3.org" in url:      # SVG 命名空间声明允许
                continue
            if re.match(r'https?://(127\.0\.0\.1|localhost)', url):
                continue                  # 本机配置值（wemm_url / hyde_llm_url），不发起请求
            problems.append("%s 存在外链（离线铁律）：%s" % (label, url))

    # ---- id 引用完整性 ----
    html_ids = set(re.findall(r'\bid="([^"]+)"', html))
    if len(re.findall(r'\bid="', html)) != len(html_ids):
        dup = [i for i in html_ids if len(re.findall(r'id="%s"' % re.escape(i), html)) > 1]
        problems.append("重复 id：%s" % dup[:6])
    for m in re.finditer(r"\$\('([^']+)'\)", app_js):
        ident = m.group(1)
        if ident.startswith("#"):
            ident = ident[1:]
        if "." in ident or " " in ident or "[" in ident:
            continue  # 复合选择器不做 id 校验
        if ident and ident not in html_ids:
            problems.append("app.js 引用了不存在的 id #%s" % ident)

    # ---- 契约方法三向对齐 ----
    methods = _contract_methods()
    called = set(re.findall(r"API\.([a-z_]+)\(", app_js))
    for m in sorted(methods):
        if not re.search(r"\b%s\s*[:(]" % re.escape(m), mock_js):
            problems.append("mock.js 缺少契约方法 %s" % m)
        if m not in called and m not in ALLOW_UNCALLED:
            problems.append("app.js 从未调用契约方法 %s" % m)
    for m in sorted(called - methods):
        problems.append("app.js 调用了契约之外的方法 %s" % m)

    return problems


if __name__ == "__main__":
    issues = run_checks()
    if issues:
        print("接线检查未通过：")
        for i in issues:
            print(" -", i)
        raise SystemExit(1)
    print("接线检查全绿：契约方法三向对齐、id 完整、无外链。")
