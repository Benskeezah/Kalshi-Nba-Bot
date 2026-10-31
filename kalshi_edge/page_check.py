"""Writes a short health check of the published report (size, sections, broken HTML)."""
import re
import sys
from html.parser import HTMLParser

VOID = {"meta", "link", "br", "img", "input", "hr"}


class _P(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.bad = [], []

    def handle_starttag(self, t, a):
        if t not in VOID:
            self.stack.append((t, self.getpos()))

    def handle_endtag(self, t):
        if not self.stack:
            self.bad.append(f"extra </{t}> at {self.getpos()}")
            return
        if self.stack[-1][0] == t:
            self.stack.pop()
            return
        self.bad.append(f"</{t}> at {self.getpos()} but <{self.stack[-1][0]}> open since {self.stack[-1][1]}")
        while self.stack and self.stack[-1][0] != t:
            self.stack.pop()
        if self.stack:
            self.stack.pop()


def check(path: str) -> str:
    src = open(path, encoding="utf-8").read()
    p = _P()
    p.feed(src)
    heads = re.findall(r"<h2>(.*?)</h2>", src)
    tables = [len(re.findall(r"<tr>", t)) for t in re.findall(r"<tbody>(.*?)</tbody>", src, re.S)]
    return "\n".join([f"size: {len(src):,} bytes", f"sections: {heads}", f"table rows: {tables}",
                      f"html problems: {p.bad[:10] or 'none'}",
                      f"left open: {[t for t, _ in p.stack][:10] or 'none'}"])


if __name__ == "__main__":
    out = check(sys.argv[1])
    print(out)
    if len(sys.argv) > 2:
        open(sys.argv[2], "w").write(out + "\n")
