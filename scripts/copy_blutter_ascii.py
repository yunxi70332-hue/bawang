from pathlib import Path
import shutil
src=Path(r'E:\霸王茶姬\tools\blutter')
dst=Path(r'C:\blutter_work')
if dst.exists(): shutil.rmtree(dst)
shutil.copytree(src,dst)
for p in [Path(r'C:\blutter_input\arm64'), Path(r'C:\blutter_out')]:
    if p.exists(): shutil.rmtree(p)
Path(r'C:\blutter_input\arm64').mkdir(parents=True)
for name in ('libapp.so','libflutter.so'):
    shutil.copy2(Path(r'E:\霸王茶姬\decompiled\raw_apk\lib\arm64-v8a')/name, Path(r'C:\blutter_input\arm64')/name)
Path(r'C:\blutter_out').mkdir()
print('copied', dst, (dst/'blutter'/'src'/'pch.h').exists())
