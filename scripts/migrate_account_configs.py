"""Convert workspace configs from full copies to overlays on configs/config.yaml.

    uv run python scripts/migrate_account_configs.py            # dry run: what changes
    uv run python scripts/migrate_account_configs.py --apply    # back up, then convert

A legacy workspace YAML is a full copy of configs/config.yaml as it was when
the workspace was created, so every default changed since then never reached
it. Converting keeps what an operator set for that workspace from the
dashboard and lets everything else follow the shared config again - which is
a behaviour change, listed here per setting before anything is written.

The dry run compares what the agent reads today (the copy, with code
defaults for missing keys) with what it will read after conversion (the
shared config plus the overlay). Nothing else is touched: no database, no
Chrome profile, no catalogue. Each converted workspace takes effect the next
time its agent starts.
"""

import argparse
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api.accounts import AccountRegistry  # noqa: E402
from load_config import EXTENDS_KEY, VERSION_KEY, deep_merge, read_yaml  # noqa: E402


def flatten(tree, prefix=""):
    out = {}
    for key, value in (tree or {}).items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def effective_after(registry, account) -> dict:
    rendered = registry.render_config(account)
    overlay = {k: v for k, v in rendered.items() if k not in (EXTENDS_KEY, VERSION_KEY)}
    return deep_merge(read_yaml(registry.base_config_path), overlay)


def short(value) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def report(registry, accounts) -> None:
    changed = defaultdict(list)   # key -> [(label, before, after)]
    added = defaultdict(list)     # key -> [new value per workspace] (code default before)
    dropped = defaultdict(list)   # key -> [label]  (nothing reads it)
    for account in accounts:
        before = flatten(read_yaml(account.config_path))
        after = flatten(effective_after(registry, account))
        label = f"{account.label} ({account.id})"
        for key in sorted(set(before) | set(after)):
            if key not in after:
                dropped[key].append(label)
            elif key not in before:
                added[key].append(after[key])
            elif before[key] != after[key]:
                changed[key].append((label, before[key], after[key]))

    print(f"Workspace dùng config kiểu cũ: {len(accounts)}\n")
    if changed:
        print("ĐỔI GIÁ TRỊ (hành vi thay đổi sau khi agent khởi động lại):")
        for key, rows in sorted(changed.items()):
            print(f"  {key}")
            for label, old, new in rows:
                print(f"    - {label}: {short(old)} -> {short(new)}")
        print()
    if added:
        print("THÊM (trước đây thiếu, agent dùng giá trị dự phòng trong code):")
        for key, values in sorted(added.items()):
            print(f"  {key} = {short(values[0])}: {len(values)} workspace")
        print()
    if dropped:
        print("BỎ (không còn trong config gốc, không code nào đọc):")
        for key, labels in sorted(dropped.items()):
            print(f"  {key}: {len(labels)} workspace")
        print()


def apply(registry, accounts) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = accounts[0].config_path.parent / f"backup-legacy-{stamp}"
    backup.mkdir(parents=True)
    for account in accounts:
        shutil.copy2(account.config_path, backup / account.config_path.name)
    backup.chmod(0o700)
    for account in accounts:
        registry.write_config(account)
        print(f"  đã chuyển {account.id}")
    print(f"\nBản gốc đã sao lưu ở {backup}")
    print("Khôi phục một workspace: chép file từ thư mục đó về configs/accounts/.")
    print("Có hiệu lực khi agent của từng workspace khởi động lại (deploy/README.md, 'Code agent').")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="sao lưu rồi chuyển đổi")
    parser.add_argument("--account", action="append", default=[],
                        help="chỉ xử lý workspace này (lặp lại được)")
    args = parser.parse_args()

    registry = AccountRegistry()
    accounts = registry.legacy_configs()
    if args.account:
        accounts = [account for account in accounts if account.id in args.account]
    if not accounts:
        print("Không có workspace nào dùng config kiểu cũ - không có gì để làm.")
        return 0
    report(registry, accounts)
    if not args.apply:
        print("Dry run - chưa ghi gì. Chạy lại với --apply để sao lưu và chuyển đổi.")
        return 0
    apply(registry, accounts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
