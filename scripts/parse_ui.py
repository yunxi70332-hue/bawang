# -*- coding: utf-8 -*-
"""Parse uiautomator dump XML: extract clickable nodes with text/desc/res-id/bounds center."""
import sys
import xml.etree.ElementTree as ET

def center(bounds):
    try:
        b = bounds.replace('[', '').replace(']', ',').split(',')
        x1, y1, x2, y2 = map(int, b[:4])
        return (x1 + x2) // 2, (y1 + y2) // 2
    except Exception:
        return None

def main(path):
    tree = ET.parse(path)
    root = tree.getroot()
    print(f"=== {path} ===")
    # summary
    nodes = list(root.iter('node'))
    print(f"total nodes: {len(nodes)}")
    idx = 0
    for n in nodes:
        clickable = n.get('clickable') == 'true'
        text = n.get('text') or ''
        desc = n.get('content-desc') or ''
        rid = n.get('resource-id') or ''
        cls = n.get('class') or ''
        if not clickable:
            continue
        # skip totally undescriptive container clicks only if nothing at all
        c = center(n.get('bounds') or '')
        idx += 1
        label = text or desc or rid or cls.rsplit('.', 1)[-1]
        print(f"[{idx:03d}] center={c} text={text!r} desc={desc!r} rid={rid} class={cls} bounds={n.get('bounds')}")
    # also show non-clickable texts for context (page title etc.)
    print("--- non-clickable with text/desc ---")
    for n in nodes:
        if n.get('clickable') == 'true':
            continue
        text = n.get('text') or ''
        desc = n.get('content-desc') or ''
        if not text and not desc:
            continue
        c = center(n.get('bounds') or '')
        print(f"    ctx center={c} text={text!r} desc={desc!r} rid={n.get('resource-id','')}")

if __name__ == '__main__':
    main(sys.argv[1])
