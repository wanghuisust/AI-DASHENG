"""Final thorough cleanup of all 'hermes' references in data/skills/
Uses str.replace for paths with backslashes to avoid re.sub escape issues."""
import os, re
from pathlib import Path

DASHENG_SKILLS = Path(r"G:\AI-DASHENG\data\skills")

# str.replace replacements (safe, no regex issues)
str_replacements = [
    ('hermes-agent', 'dasheng-agent'),
    ('hermes_agent', 'dasheng_agent'),
    ('.hermes/', '.dasheng/'),
    ('.hermes\\', '.dasheng\\'),
    ('/.hermes/', '/.dasheng/'),
    ('HermesAgent', 'DASHENGAgent'),
    ('Hermes', 'DASHENG'),
    ('HERMES', 'DASHENG'),
    ('hermes', 'dasheng'),
]

count = 0
for root, dirs, files in os.walk(DASHENG_SKILLS):
    if '.git' in root:
        continue
    for fname in files:
        fpath = Path(root) / fname
        if fname.endswith(('.png', '.jpg', '.jpeg', '.gif', '.ico', '.exe', '.dll', '.zip', '.wav', '.mp3', '.mp4', '.xsd', '.sty', '.bst', '.cls', '.pdf')):
            continue
        try:
            text = fpath.read_text(encoding='utf-8', errors='replace')
        except:
            continue
        
        original = text
        for old, new in str_replacements:
            text = text.replace(old, new)
        
        if text != original:
            fpath.write_text(text, encoding='utf-8')
            rel = fpath.relative_to(DASHENG_SKILLS)
            print(f"  FIXED: {rel}")
            count += 1

# Final verification
remaining = []
for root, dirs, files in os.walk(DASHENG_SKILLS):
    if '.git' in root:
        continue
    for fname in files:
        fpath = Path(root) / fname
        if fname.endswith(('.png', '.jpg', '.jpeg', '.gif', '.ico', '.exe', '.dll', '.zip', '.wav', '.mp3', '.mp4', '.xsd', '.sty', '.bst', '.cls', '.pdf')):
            continue
        try:
            text = fpath.read_text(encoding='utf-8', errors='replace')
            if 'hermes' in text.lower():
                n = text.lower().count('hermes')
                remaining.append((str(fpath.relative_to(DASHENG_SKILLS)), n))
        except:
            pass

print(f"\nFixed {count} files")
print(f"Remaining files with 'hermes': {len(remaining)}")
for f, n in remaining:
    print(f"  ! {f} ({n}x)")
