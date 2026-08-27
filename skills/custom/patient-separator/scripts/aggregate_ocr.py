#!/usr/bin/env python3
"""按 patient_index.json 把分页 OCR 聚合成每位患者每来源一份 ocr_records.md。

规则见 references/aggregate-ocr.md：只读 patient_index.json，只写 ocr_records.md；
.md 优先、.txt 回退、禁止通配拼接、只拼已登记页、页块起始行（「来源图片」）原样保留。
缺页只报告不补 OCR（覆盖率门禁由 /pdf-image-extractor 的 ocr_coverage.py 负责）。
"""
import argparse
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--workspace', required=True, help='workspace 根目录（含 patient_index.json / ocr/ / patients/）')
    ap.add_argument('--patient', default=None, help='只聚合该 patient_id；缺省聚合全部')
    args = ap.parse_args()

    ws = Path(args.workspace)
    index = json.loads((ws / 'patient_index.json').read_text(encoding='utf-8'))
    for patient in index:
        pid = patient['patient_id']
        if args.patient and pid != args.patient:
            continue
        for sf in patient['source_files']:
            source = sf['source_name']
            out_dir = ws / 'patients' / pid / 'ocr' / source
            out_dir.mkdir(parents=True, exist_ok=True)
            parts = []
            missing = []
            for p in sf['pages']:
                stem = f'{source}_page_{p:03d}'
                md = ws / 'ocr' / source / f'{stem}.md'
                txt = ws / 'images' / source / f'{stem}.txt'
                if md.exists():
                    parts.append(md.read_text(encoding='utf-8'))
                elif txt.exists():
                    parts.append(f'（来源图片：{txt} 文本层）\n' + txt.read_text(encoding='utf-8'))
                else:
                    missing.append(p)
            out_file = out_dir / 'ocr_records.md'
            out_file.write_text('\n\n'.join(parts), encoding='utf-8')
            print(f'{pid}/{source}/ocr_records.md: {len(parts)} pages, {len(missing)} missing')
            if missing:
                print(f'  missing pages: {missing}')


if __name__ == '__main__':
    main()
