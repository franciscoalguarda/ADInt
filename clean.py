"""Remove service databases, Python caches and generated printing output.

Stop the services before running.
"""

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


def get_cleanup_targets(directory, printing_output=None, databases_only=False):
    for entry in directory.iterdir():
        reject_link(entry)
        if entry.is_dir():
            # Inspect all descendants before selecting a directory for removal.
            nested_targets = list(get_cleanup_targets(
                entry, printing_output, databases_only=databases_only
            ))
            if not databases_only and (entry.name == "__pycache__" or entry == printing_output):
                yield entry
            else:
                yield from nested_targets
        elif entry.name.endswith(DATABASE_SUFFIXES):
            yield entry


def clean(project_root, dry_run=False, *, databases_only=False, output_only=False, services=None):
    if databases_only and output_only:
        raise ValueError("Choose either databases-only or output-only cleanup.")
    if services is None:
        selected_services = ("printing_service",) if output_only else SERVICES
    else:
        selected_services = tuple(dict.fromkeys(services))
    if any(service not in SERVICES for service in selected_services):
        raise ValueError("Unknown service selection.")
    if output_only and any(service != "printing_service" for service in selected_services):
        raise ValueError("Output-only cleanup applies only to printing_service.")

    targets = []
    for service in selected_services:
        service_path = project_root / service
        if not service_path.exists():
            continue
        reject_link(service_path)
        service_root = service_path.resolve(strict=True)
        printing_output = service_root / "output" if service == "printing_service" else None
        if output_only:
            if not printing_output.exists():
                continue
            reject_link(printing_output)
            if not printing_output.is_dir():
                raise ValueError(f"Output path is not a directory: {printing_output}")
            # Validate descendants before selecting the entire output directory.
            list(get_cleanup_targets(printing_output))
            service_targets = [printing_output]
        else:
            service_targets = get_cleanup_targets(
                service_root, printing_output, databases_only=databases_only
            )
        for target in service_targets:
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
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--databases-only", action="store_true",
        help="Remove only SQLite databases and their journal/WAL/SHM files."
    )
    modes.add_argument(
        "--output-only", action="store_true",
        help="Remove only printing_service/output, preserving databases and caches."
    )
    parser.add_argument(
        "--service", choices=SERVICES, action="append", dest="services",
        help="Limit cleanup to this service. Repeat to select multiple services."
    )
    args = parser.parse_args()
    if args.output_only and args.services and any(
        service != "printing_service" for service in args.services
    ):
        parser.error("--output-only can only be combined with --service printing_service")
    try:
        clean(
            Path(__file__).resolve().parent,
            dry_run=args.dry_run,
            databases_only=args.databases_only,
            output_only=args.output_only,
            services=args.services,
        )
    except (OSError, ValueError) as error:
        parser.exit(1, f"Cleanup failed: {error}\nStop all service processes before cleaning.\n")


if __name__ == "__main__":
    main()
