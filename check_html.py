from hitl.dashboard import _HTML
idx = _HTML.find("decide(")
broken = []
fixed = []
while idx != -1:
    snippet = _HTML[idx:idx+60]
    if "approve" in snippet or "reject" in snippet:
        actual = snippet  # This is already the actual content
        # Check if it has \' (broken) or \\\' (fixed backslash+quote)
        if "\\'" in snippet:
            fixed.append((idx, snippet))
        else:
            broken.append((idx, snippet))
    idx = _HTML.find("decide(", idx+1)

print("BROKEN (no backslash before quote):")
for pos, s in broken:
    print("  pos %d: %r" % (pos, s[:80]))
print("\nFIXED (backslash before quote):")
for pos, s in fixed:
    print("  pos %d: %r" % (pos, s[:80]))
print("\nTotal broken: %d, Total fixed: %d" % (len(broken), len(fixed)))
