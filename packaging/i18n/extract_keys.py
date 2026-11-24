import ast, glob, json, re, sys
HAN = re.compile(r"[㐀-鿿]")
keys = {}
def add(k, src):
    k = k.strip()
    if k and HAN.search(k): keys.setdefault(k, src)
for f in glob.glob("src/papermap/web/templates/*.html"):
    s = open(f, encoding="utf-8").read()
    for m in re.finditer(r"""_\(\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""", s):
        add(ast.literal_eval(m.group(1)), f)
for f in glob.glob("src/papermap/web/static/*.js") + glob.glob("src/papermap/web/templates/*.html"):
    s = open(f, encoding="utf-8").read()
    for m in re.finditer(r"""(?<![\w$.])t\(\s*("(?:[^"\\\n]|\\.)*")""", s):
        add(json.loads(m.group(1)), f)
py = ["src/papermap/web/app.py", "src/papermap/web/evidence_routes.py", "src/papermap/web/plugin_admin.py",
      "src/papermap/updates.py", "src/papermap/gaps.py", "src/papermap/tracking.py", "src/papermap/web/tracking_routes.py", "src/papermap/evidence.py", "src/papermap/questions.py", "src/papermap/themes.py",
      "src/papermap/backup.py", "src/papermap/supplements.py", "src/papermap/consolidate.py", "src/papermap/desktop.py",
      "src/papermap/chat.py", "src/papermap/web/chat_routes.py", "src/papermap/versions.py", "src/papermap/purposes.py",
      "src/papermap/trash.py", "src/papermap/evidence_links.py", "src/papermap/writing.py", "src/papermap/citecheck.py",
      "src/papermap/clips.py", "src/papermap/web/writing_routes.py", "src/papermap/web/extras_routes.py", "src/papermap/appupdate.py", "src/papermap/feedback.py", "src/papermap/demo.py"]
for f in py:
    for node in ast.walk(ast.parse(open(f, encoding="utf-8").read())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 300 and "\n" not in node.value:
            add(node.value, f)
json.dump(keys, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=0)
print(len(keys), sum(len(k) for k in keys))
