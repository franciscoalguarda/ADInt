"""Remove service databases and Python caches. Stop the services before running."""

import argparse
import shutil
import stat
from pathlib import Path


SERVICES = (
    "user_service",
    "admin_service",
    "printing_service",
    "correction_service",
)
DATABASE_SUFFIXES = (".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal")


def reject_link(path):
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    if path.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"Cleanup stopped: linked path at {path}")


def get_cleanup_targets(directory):
    for entry in directory.iterdir():
        reject_link(entry)
        if entry.is_dir():
            # Inspect all descendants before selecting a directory for removal.
            nested_targets = list(get_cleanup_targets(entry))
            if entry.name == "__pycache__":
                yield entry
            else:
                yield from nested_targets
        elif entry.name.endswith(DATABASE_SUFFIXES):
            yield entry


def clean(project_root, dry_run=False):
    targets = []
    for service in SERVICES:
        service_path = project_root / service
        if not service_path.exists():
            continue
        reject_link(service_path)
        service_root = service_path.resolve(strict=True)
        for target in get_cleanup_targets(service_root):
            target = target.resolve(strict=True)
            target.relative_to(service_root)
            targets.append(target)

    # Every target has been resolved and checked before any deletion starts.
    for target in targets:
        if dry_run:
            print(f"Would remove: {target}")
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        print(f"Removed: {target}")

    if not targets:
        print("Nothing to clean.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="List targets without deleting them."
    )
    args = parser.parse_args()
    try:
        clean(Path(__file__).resolve().parent, dry_run=args.dry_run)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Cleanup failed: {error}\nStop all service processes before cleaning.\n")


if __name__ == "__main__":
    main()
