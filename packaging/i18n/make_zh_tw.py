"""Regenerate locales/zh-TW.json from the extracted keys with OpenCC (s2twp: Taiwan wording).
Dev-only: pip install opencc-python-reimplemented==0.1.7 in a scratch venv, then
  python packaging/i18n/extract_keys.py /tmp/keys.json && python packaging/i18n/make_zh_tw.py /tmp/keys.json"""
import json
import sys

from opencc import OpenCC

FIX = {"型別": "類型", "控制檯": "控制台", "賬號": "帳號", "賬戶": "帳戶", "賬單": "帳單"}  # s2twp slips
cc = OpenCC("s2twp")
out = {}
for k in sorted(json.load(open(sys.argv[1], encoding="utf-8"))):
    v = cc.convert(k)
    for a, b in FIX.items():
        v = v.replace(a, b)
    out[k] = v
json.dump(out, open("src/papermap/locales/zh-TW.json", "w", encoding="utf-8"), ensure_ascii=False, indent=0, sort_keys=True)
print(len(out))
