#!/usr/bin/env python3
"""
disk_tree.py - Inventory every file and folder under a path and write a
structured tree document with exact sizes in bytes.

Usage:
    python3 disk_tree.py /path/to/scan                 # writes disk_inventory.md
    python3 disk_tree.py /home/you -o report.md
    python3 disk_tree.py /home/you -o report.txt --format txt
    python3 disk_tree.py /home/you --max-depth 3 --exclude .cache --exclude node_modules

Notes:
  * Sizes are the exact apparent size in bytes (st_size), the same figure
    "ls -l" and "du --apparent-size -b" show.  Folder sizes are the sum of
    everything beneath them.
  * Symbolic links are listed but never followed, so nothing is counted twice
    and the scan cannot loop.  Their size is the link itself (not the target).
  * Folders that cannot be read are listed under "Unreadable" and the scan
    continues.
  * Needs only the Python 3 standard library (default on Debian).
"""

import argparse
import datetime as _dt
import fnmatch
import os
import stat
import sys

# ---------------------------------------------------------------- data model


class Node:
    __slots__ = ("name", "path", "kind", "size", "children", "file_count",
                 "dir_count", "mtime", "link_target", "error")

    def __init__(self, name, path, kind):
        self.name = name
        self.path = path
        self.kind = kind            # "dir", "file", "link", "other"
        self.size = 0               # bytes (own size for files, total for dirs)
        self.children = []
        self.file_count = 0         # files beneath this node (dirs only)
        self.dir_count = 0          # sub-folders beneath this node (dirs only)
        self.mtime = None
        self.link_target = None
        self.error = None


# ---------------------------------------------------------------- scanning


def _excluded(name, patterns):
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def scan(path, exclude=(), max_depth=None, _depth=0, _errors=None, _excluded_log=None):
    """Recursively build a Node tree for `path`."""
    if _errors is None:
        _errors = []
    if _excluded_log is None:
        _excluded_log = []

    st = os.lstat(path)
    name = os.path.basename(path.rstrip(os.sep)) or path
    node = Node(name, path, "dir")
    node.mtime = st.st_mtime

    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
    except OSError as exc:
        node.error = f"{exc.strerror or exc}"
        _errors.append((path, node.error))
        return node

    for entry in entries:
        if _excluded(entry.name, exclude):
            _excluded_log.append(entry.path)
            continue
        try:
            est = entry.stat(follow_symlinks=False)
        except OSError as exc:
            child = Node(entry.name, entry.path, "other")
            child.error = exc.strerror or str(exc)
            _errors.append((entry.path, child.error))
            node.children.append(child)
            continue

        mode = est.st_mode
        if stat.S_ISLNK(mode):
            child = Node(entry.name, entry.path, "link")
            try:
                child.link_target = os.readlink(entry.path)
            except OSError:
                child.link_target = "?"
            child.size = est.st_size
            child.mtime = est.st_mtime
        elif stat.S_ISDIR(mode):
            if max_depth is not None and _depth + 1 >= max_depth:
                child = Node(entry.name, entry.path, "dir")
                child.mtime = est.st_mtime
                child.error = "not expanded (max depth)"
                # still total its size so the figure is honest
                child.size, child.file_count, child.dir_count = _total_only(
                    entry.path, exclude, _errors)
            else:
                child = scan(entry.path, exclude, max_depth, _depth + 1,
                             _errors, _excluded_log)
        elif stat.S_ISREG(mode):
            child = Node(entry.name, entry.path, "file")
            child.size = est.st_size
            child.mtime = est.st_mtime
            child.file_count = 1
        else:
            child = Node(entry.name, entry.path, "other")
            child.size = est.st_size
            child.mtime = est.st_mtime

        node.children.append(child)

    # roll up totals for this folder
    for c in node.children:
        node.size += c.size
        if c.kind == "dir":
            node.file_count += c.file_count
            node.dir_count += 1 + c.dir_count
        elif c.kind in ("file", "link", "other"):
            node.file_count += c.file_count
    return node


def _total_only(path, exclude, errors):
    """Size/count a folder without keeping its tree (used past max depth)."""
    size = files = dirs = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            it = list(os.scandir(cur))
        except OSError as exc:
            errors.append((cur, exc.strerror or str(exc)))
            continue
        for e in it:
            if _excluded(e.name, exclude):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError as exc:
                errors.append((e.path, exc.strerror or str(exc)))
                continue
            if stat.S_ISDIR(st.st_mode):
                dirs += 1
                stack.append(e.path)
            else:
                size += st.st_size
                if stat.S_ISREG(st.st_mode):
                    files += 1
    return size, files, dirs


# ---------------------------------------------------------------- formatting


def human(n):
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{int(f)} B" if u == "B" else f"{f:.2f} {u}"
        f /= 1024
    return str(n)


def _fmt_time(ts):
    if ts is None:
        return ""
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _label(node, root_path):
    if node.kind == "dir":
        tag = f"[DIR] {node.name}/"
    elif node.kind == "link":
        tag = f"[LNK] {node.name} -> {node.link_target}"
    elif node.kind == "file":
        tag = f"[FILE] {node.name}"
    else:
        tag = f"[OTHER] {node.name}"
    return tag


def render_tree_lines(node, prefix="", is_last=True, is_root=True, out=None):
    """Yield text lines for a box-drawing tree."""
    if out is None:
        out = []
    if is_root:
        connector, child_prefix = "", ""
    else:
        connector = "└── " if is_last else "├── "
        child_prefix = prefix + ("    " if is_last else "│   ")

    size_txt = f"{node.size:>16,d} B"
    if node.kind == "dir":
        extra = f"  ({node.file_count:,} files, {node.dir_count:,} folders)"
        if node.error:
            extra += f"  !! {node.error}"
    else:
        extra = f"  ({_fmt_time(node.mtime)})" if node.mtime else ""
        if node.error:
            extra += f"  !! {node.error}"
    label = _label(node, None) if not is_root else (node.path + "/")
    out.append(f"{prefix}{connector}{label}  |  {size_txt}{extra}")

    n = len(node.children)
    for i, child in enumerate(node.children):
        render_tree_lines(child, child_prefix, i == n - 1, False, out)
    return out


def collect_errors(node, acc=None):
    if acc is None:
        acc = []
    if node.error and node.kind == "dir" and "max depth" not in node.error:
        acc.append((node.path, node.error))
    for c in node.children:
        if c.error and "max depth" not in c.error:
            acc.append((c.path, c.error))
        collect_errors(c, acc)
    return acc


def write_report(root, out_path, fmt, args, errors, excluded_count):
    lines = []
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    host = os.uname().nodename if hasattr(os, "uname") else "unknown"
    title = f"Disk inventory: {root.path}"

    summary = [
        ("Scanned root", root.path),
        ("Host", host),
        ("Generated", now),
        ("Total size (bytes)", f"{root.size:,}"),
        ("Total size (readable)", human(root.size)),
        ("Files", f"{root.file_count:,}"),
        ("Folders", f"{root.dir_count:,}"),
        ("Excluded entries", f"{excluded_count:,}"),
        ("Errors", f"{len(errors):,}"),
        ("Max depth", str(args.max_depth) if args.max_depth else "unlimited"),
    ]

    if fmt == "md":
        lines.append(f"# {title}\n")
        lines.append("## Summary\n")
        lines.append("| Item | Value |")
        lines.append("|---|---|")
        for k, v in summary:
            lines.append(f"| {k} | {v} |")
        lines.append("\n## Tree\n")
        lines.append("Each line: name | exact size in bytes | (folder counts) or (modified time).\n")
        lines.append("```text")
        lines += render_tree_lines(root)
        lines.append("```\n")
        lines.append("## Largest top-level folders\n")
        lines.append("| Folder | Bytes | Readable |")
        lines.append("|---|---:|---|")
        for c in sorted(root.children, key=lambda c: c.size, reverse=True):
            if c.kind == "dir":
                lines.append(f"| {c.name}/ | {c.size:,} | {human(c.size)} |")
        lines.append("")
        lines.append("## Unreadable or errored entries\n")
        if errors:
            lines.append("| Path | Reason |")
            lines.append("|---|---|")
            for p, e in errors:
                lines.append(f"| `{p}` | {e} |")
        else:
            lines.append("None.")
    else:
        lines.append(title)
        lines.append("=" * len(title))
        for k, v in summary:
            lines.append(f"{k:<24}: {v}")
        lines.append("")
        lines += render_tree_lines(root)
        lines.append("")
        lines.append("Unreadable or errored entries:")
        if errors:
            for p, e in errors:
                lines.append(f"  {p}  ({e})")
        else:
            lines.append("  None.")

    text = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return out_path


# ---------------------------------------------------------------- main


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="folder to scan (e.g. /home/you)")
    ap.add_argument("-o", "--output", default="disk_inventory.md",
                    help="report file to write (default: disk_inventory.md)")
    ap.add_argument("--format", choices=["md", "txt"], default=None,
                    help="report format; default is taken from the output extension")
    ap.add_argument("--exclude", action="append", default=[],
                    help="name pattern to skip (glob, repeatable), e.g. --exclude .cache")
    ap.add_argument("--max-depth", type=int, default=None,
                    help="expand folders only this many levels deep (sizes still complete)")
    args = ap.parse_args(argv)

    root_path = os.path.abspath(args.root)
    if not os.path.isdir(root_path):
        print(f"error: not a folder: {root_path}", file=sys.stderr)
        return 2

    fmt = args.format or ("txt" if args.output.lower().endswith(".txt") else "md")

    errors = []
    excluded_log = []
    root = scan(root_path, tuple(args.exclude), args.max_depth,
                _errors=errors, _excluded_log=excluded_log)
    errors = collect_errors(root) or errors
    # de-duplicate while keeping order
    seen, uniq = set(), []
    for p, e in errors:
        if p not in seen:
            seen.add(p)
            uniq.append((p, e))

    out = write_report(root, args.output, fmt, args, uniq, len(excluded_log))
    print(f"Wrote {out}")
    print(f"Files: {root.file_count:,}  Folders: {root.dir_count:,}  "
          f"Total: {root.size:,} bytes  Errors: {len(uniq):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
