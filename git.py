"""
Your Own Git - a small Git-like version-control tool.

This project uses Git's object format (SHA-1 + zlib) for blobs, trees,
commits and tags. Its index is a simple JSON file, so it is NOT fully
compatible with Git's native index.
"""

import argparse
import configparser
import fnmatch
import hashlib
import json
import os
import re
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path



# Repository and paths


class GitError(Exception):
    pass


class Repository:
    def __init__(self, worktree: Path):
        self.worktree = worktree.resolve()
        self.gitdir = self.worktree / ".git"
        if not self.gitdir.is_dir():
            raise GitError(f"Not a Your Own Git repository: {self.worktree}")
        self.config = configparser.ConfigParser(strict=False, interpolation=None,
                                                allow_no_value=True)
        config_path = self.gitdir / "config"
        if config_path.exists():
            try:
                self.config.read(config_path, encoding="utf-8")
            except (configparser.Error, UnicodeDecodeError):
                pass  # the config is not required for the commands below

    def git_path(self, *parts):
        return self.gitdir.joinpath(*parts)

    def object_path(self, sha):
        return self.gitdir / "objects" / sha[:2] / sha[2:]


def find_repo(start="."):
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    while True:
        if (current / ".git").is_dir():
            return Repository(current)
        parent = current.parent
        if parent == current:
            raise GitError("No repository found. Run 'init' first.")
        current = parent


def init_repo(path="."):
    root = Path(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    gitdir = root / ".git"
    if gitdir.exists() and any(gitdir.iterdir()):
        raise GitError(f"{gitdir} already exists and is not empty.")
    for folder in ("objects", "refs/heads", "refs/tags", "refs/remotes", "info"):
        (gitdir / folder).mkdir(parents=True, exist_ok=True)

    (gitdir / "description").write_text(
        "Unnamed repository; edit this file to name the repository.\n",
        encoding="utf-8"
    )
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    config = configparser.ConfigParser()
    config["core"] = {
        "repositoryformatversion": "0",
        "filemode": "false",
        "bare": "false",
    }
    with (gitdir / "config").open("w", encoding="utf-8") as f:
        config.write(f)
    (gitdir / "index").write_text("[]", encoding="utf-8")
    print(f"Initialized empty Your Own Git repository in {gitdir}")


def to_relative(repo, raw_path):
    """Return (absolute path, posix path relative to the worktree)."""
    path = Path(raw_path).resolve()
    try:
        rel = path.relative_to(repo.worktree).as_posix()
    except ValueError:
        raise GitError(f"Path is outside the repository: {raw_path}")
    if rel == ".":
        rel = ""
    return path, rel


def check_ref_name(name):
    if (not name or name.startswith(("/", "-", ".")) or name.endswith(("/", ".", ".lock"))
            or ".." in name or "//" in name
            or any(c in name for c in " ~^:?*[\\@{")
            or any(ord(c) < 32 for c in name)):
        raise GitError(f"Invalid reference name: {name!r}")



# .gitignore handling


def load_ignore_patterns(repo):
    ignore_file = repo.worktree / ".gitignore"
    if not ignore_file.is_file():
        return []
    patterns = []
    for line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.rstrip()
        if line and not line.lstrip().startswith("#"):
            patterns.append(line.strip())
    return patterns


def is_ignored(patterns, rel_path, is_dir=False):
    """Simplified .gitignore matching (supports *, ?, [], !, leading / and trailing /)."""
    parts = rel_path.split("/")
    ignored = False
    for raw in patterns:
        negate = raw.startswith("!")
        pat = raw[1:] if negate else raw
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        if not pat:
            continue
        anchored = pat.startswith("/") or "/" in pat
        pat = pat.lstrip("/")
        matched = False
        # Test every prefix: if a parent directory matches, its content is ignored too.
        for i in range(1, len(parts) + 1):
            last = i == len(parts)
            if dir_only and last and not is_dir:
                continue
            target = "/".join(parts[:i]) if anchored else parts[i - 1]
            if fnmatch.fnmatch(target, pat):
                matched = True
                break
        if matched:
            ignored = not negate
    return ignored


def iter_worktree_files(repo, start=None, patterns=None):
    """Yield relative posix paths of non-ignored files (never enters .git)."""
    start = start or repo.worktree
    if patterns is None:
        patterns = load_ignore_patterns(repo)
    for root, dirs, files in os.walk(start):
        root_path = Path(root)
        kept = []
        for d in sorted(dirs):
            if d == ".git":
                continue
            rel = (root_path / d).relative_to(repo.worktree).as_posix()
            if not is_ignored(patterns, rel, True):
                kept.append(d)
        dirs[:] = kept
        for f in sorted(files):
            rel = (root_path / f).relative_to(repo.worktree).as_posix()
            if not is_ignored(patterns, rel, False):
                yield rel



# Git objects


def write_object(repo, obj_type, data):
    header = obj_type.encode("ascii") + b" " + str(len(data)).encode("ascii") + b"\0"
    raw = header + data
    sha = hashlib.sha1(raw).hexdigest()
    path = repo.object_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(zlib.compress(raw))
    return sha


def read_object(repo, sha):
    if not sha or len(sha) != 40:
        raise GitError(f"Invalid object ID: {sha}")
    path = repo.object_path(sha)
    if not path.is_file():
        raise GitError(f"Object not found: {sha}")
    try:
        raw = zlib.decompress(path.read_bytes())
    except zlib.error as exc:
        raise GitError(f"Corrupt object {sha}: {exc}") from exc
    split = raw.find(b" ")
    nul = raw.find(b"\0", split)
    if split < 0 or nul < 0:
        raise GitError(f"Malformed object: {sha}")
    obj_type = raw[:split].decode("ascii")
    size = int(raw[split + 1:nul])
    data = raw[nul + 1:]
    if len(data) != size:
        raise GitError(f"Malformed object {sha}: incorrect size")
    return obj_type, data


def blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def hash_file(path, repo=None, write=False):
    data = Path(path).read_bytes()
    if write:
        if repo is None:
            raise GitError("A repository is required with -w.")
        return write_object(repo, "blob", data)
    return blob_sha(data)



# References and index


def read_ref(repo, ref, _depth=0):
    if _depth > 10:
        raise GitError(f"Symbolic reference loop: {ref}")
    path = repo.gitdir / ref
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    if value.startswith("ref: "):
        return read_ref(repo, value[5:], _depth + 1)
    return value or None


def head_ref(repo):
    value = (repo.gitdir / "HEAD").read_text(encoding="utf-8").strip()
    return value[5:] if value.startswith("ref: ") else None


def head_sha(repo):
    return read_ref(repo, "HEAD")


def write_ref(repo, ref, sha):
    path = repo.gitdir / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha + "\n", encoding="utf-8")


def resolve_object(repo, name):
    if name == "HEAD":
        sha = head_sha(repo)
        if not sha:
            raise GitError("HEAD does not point to a commit yet.")
        return sha

    if len(name) >= 4 and all(c in "0123456789abcdefABCDEF" for c in name):
        prefix = name.lower()
        matches = []
        objects_dir = repo.gitdir / "objects" / prefix[:2]
        if objects_dir.is_dir():
            matches = [prefix[:2] + p.name for p in objects_dir.iterdir()
                       if p.name.startswith(prefix[2:])]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise GitError(f"Ambiguous object name: {name}")

    candidates = [f"refs/heads/{name}", f"refs/tags/{name}", f"refs/remotes/{name}"]
    if name.startswith("refs/"):
        candidates.insert(0, name)
    for ref in candidates:
        if ".." in ref:
            continue
        sha = read_ref(repo, ref)
        if sha:
            return sha
    raise GitError(f"No such reference or object: {name}")


def load_index(repo):
    path = repo.gitdir / "index"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {item["path"]: item["sha"] for item in data}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GitError(f"Index is damaged: {exc}") from exc


def save_index(repo, index):
    rows = [{"path": name, "sha": sha} for name, sha in sorted(index.items())]
    (repo.gitdir / "index").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )



# Tree and commit serialization


def tree_entries(repo, tree_sha):
    obj_type, data = read_object(repo, tree_sha)
    if obj_type != "tree":
        raise GitError(f"Expected tree object, got {obj_type}")
    result = []
    pos = 0
    while pos < len(data):
        space = data.find(b" ", pos)
        nul = data.find(b"\0", space)
        if space < 0 or nul < 0 or nul + 21 > len(data):
            raise GitError("Malformed tree object.")
        mode = data[pos:space].decode("ascii")
        name = data[space + 1:nul].decode("utf-8")
        sha = data[nul + 1:nul + 21].hex()
        result.append((mode, name, sha))
        pos = nul + 21
    return result


def is_tree_mode(mode):
    # Git writes "40000"; some tools write "040000".
    return mode.lstrip("0").startswith("4")


def flatten_tree(repo, tree_sha, prefix=""):
    result = {}
    for mode, name, sha in tree_entries(repo, tree_sha):
        path = f"{prefix}/{name}" if prefix else name
        if is_tree_mode(mode):
            result.update(flatten_tree(repo, sha, path))
        else:
            result[path] = sha
    return result


def build_tree(repo, index):
    # Build nested directory dictionaries from the flat index.
    root = {}
    for path, sha in index.items():
        parts = Path(path).as_posix().split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise GitError(f"Conflict: '{part}' is both a file and a directory ({path}).")
        if isinstance(node.get(parts[-1]), dict):
            raise GitError(f"Conflict: '{path}' is both a file and a directory.")
        node[parts[-1]] = sha

    def write_node(node):
        entries = []
        for name, value in node.items():
            if isinstance(value, dict):
                child_sha = write_node(value)
                mode = b"40000"
            else:
                child_sha = value
                mode = b"100644"
            entries.append((name, mode, child_sha))

        # Git tree ordering places directories as if their names ended in '/'.
        entries.sort(key=lambda item: ((item[0] + "/") if item[1] == b"40000"
                                       else item[0]).encode("utf-8"))
        data = bytearray()
        for name, mode, sha in entries:
            data.extend(mode + b" " + name.encode("utf-8") + b"\0")
            data.extend(bytes.fromhex(sha))
        return write_object(repo, "tree", bytes(data))

    return write_node(root)


def parse_commit(data):
    header, _, message = data.partition(b"\n\n")
    fields = {}
    for line in header.splitlines():
        key, _, value = line.partition(b" ")
        fields.setdefault(key.decode("ascii", errors="replace"), []).append(
            value.decode("utf-8", errors="replace"))
    return fields, message.decode("utf-8", errors="replace").strip()


def commit_tree_sha(repo, sha):
    typ, data = read_object(repo, sha)
    if typ != "commit":
        raise GitError(f"{sha} is not a commit.")
    fields, _ = parse_commit(data)
    return fields["tree"][0]


def head_tree_files(repo):
    parent = head_sha(repo)
    if not parent:
        return {}
    return flatten_tree(repo, commit_tree_sha(repo, parent))


def format_identity_date(identity):
    """'Name <mail> 1700000000 +0100' -> 'Tue Nov 14 23:13:20 2023 +0100'."""
    try:
        _, ts, tz = identity.rsplit(" ", 2)
        sign = -1 if tz[0] == "-" else 1
        offset = timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])))
        return datetime.fromtimestamp(int(ts), offset).strftime("%a %b %d %H:%M:%S %Y ") + tz
    except (ValueError, IndexError):
        return identity



# User identity


def parse_user_from_config(path):
    """Read user.name / user.email from a Git config file (tolerant parser)."""
    if not path.is_file():
        return None, None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GitError(f"Cannot read Git configuration {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")

    name = email = None
    in_user = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            end = line.find("]")
            header = line[1:end] if end > 0 else line[1:]
            in_user = header.strip().lower() == "user"
            continue
        if in_user and "=" in line:
            key, _, value = line.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if value.startswith('"') and value.count('"') >= 2:
                value = value[1:value.rindex('"')]
            else:
                value = re.sub(r"\s+[#;].*$", "", value).strip()
            if key == "name" and value:
                name = value
            elif key == "email" and value:
                email = value
    return name, email


def get_user(repo=None):
    name = os.environ.get("GIT_AUTHOR_NAME")
    email = os.environ.get("GIT_AUTHOR_EMAIL")

    home = Path.home()
    files = []
    if repo is not None:
        files.append(repo.gitdir / "config")
    files += [home / ".gitconfig", home / ".config" / "git" / "config"]

    for file_path in files:
        n, e = parse_user_from_config(file_path)
        name = name or n
        email = email or e

    if not name or not email:
        raise GitError(
            "Git identity is not configured. Run:\n"
            '  git config --global user.name "Your Name"\n'
            '  git config --global user.email "you@example.com"\n'
            "or set GIT_AUTHOR_NAME and GIT_AUTHOR_EMAIL."
        )
    return f"{name} <{email}>"


def create_commit(repo, tree_sha, parent_sha, message):
    if not message or not message.strip():
        raise GitError("Commit message cannot be empty. Use -m \"message\".")
    author = get_user(repo)
    now = datetime.now().astimezone()
    timestamp = int(now.timestamp())
    offset = now.strftime("%z") or "+0000"
    identity = f"{author} {timestamp} {offset}"
    lines = [f"tree {tree_sha}"]
    if parent_sha:
        lines.append(f"parent {parent_sha}")
    lines.extend([f"author {identity}", f"committer {identity}"])
    data = ("\n".join(lines) + "\n\n" + message.strip() + "\n").encode("utf-8")
    return write_object(repo, "commit", data)



# Commands


def cmd_init(args):
    init_repo(args.path)


def cmd_add(args):
    repo = find_repo()
    index = load_index(repo)
    patterns = load_ignore_patterns(repo)
    for raw_path in args.paths:
        path, rel = to_relative(repo, raw_path)
        if rel == ".git" or rel.startswith(".git/"):
            raise GitError(f"Cannot add files inside .git: {raw_path}")

        if path.is_dir():
            prefix = rel + "/" if rel else ""
            for file_rel in iter_worktree_files(repo, path, patterns):
                index[file_rel] = write_object(
                    repo, "blob", (repo.worktree / file_rel).read_bytes())
                print(f"added: {file_rel}")
            # Stage deletions of tracked files that disappeared from this directory.
            for tracked in sorted(index):
                if tracked.startswith(prefix) and not (repo.worktree / tracked).exists():
                    del index[tracked]
                    print(f"removed: {tracked}")
        elif path.is_file():
            index[rel] = write_object(repo, "blob", path.read_bytes())
            print(f"added: {rel}")
        elif rel in index:
            del index[rel]
            print(f"removed: {rel}")
        else:
            raise GitError(f"Path does not match any file: {raw_path}")
    save_index(repo, index)


def cmd_rm(args):
    repo = find_repo()
    index = load_index(repo)
    for raw_path in args.paths:
        path, rel = to_relative(repo, raw_path)
        if rel not in index:
            raise GitError(f"Path is not tracked in the index: {raw_path}")
        del index[rel]
        if not args.cached and path.is_file():
            path.unlink()
        print(f"removed: {rel}")
    save_index(repo, index)


def cmd_commit(args):
    repo = find_repo()
    index = load_index(repo)
    parent = head_sha(repo)
    if not index and not parent:
        raise GitError("Nothing to commit: the index is empty.")
    tree_sha = build_tree(repo, index)
    if parent and commit_tree_sha(repo, parent) == tree_sha:
        raise GitError("Nothing to commit: no changes staged.")
    sha = create_commit(repo, tree_sha, parent, args.message)
    ref = head_ref(repo)
    if ref:
        write_ref(repo, ref, sha)
    else:
        (repo.gitdir / "HEAD").write_text(sha + "\n", encoding="utf-8")
    print(f"[{sha[:7]}] {args.message.strip().splitlines()[0]}")


def cmd_status(args):
    repo = find_repo()
    index = load_index(repo)
    current = head_tree_files(repo)

    ref = head_ref(repo)
    if ref:
        branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        print(f"On branch {branch}")
    else:
        sha = head_sha(repo)
        print(f"HEAD detached at {sha[:7] if sha else '(unknown)'}")
    if not head_sha(repo):
        print("\nNo commits yet")

    staged = []
    for path, sha in sorted(index.items()):
        if path not in current:
            staged.append(("new file", path))
        elif current[path] != sha:
            staged.append(("modified", path))
    for path in sorted(current):
        if path not in index:
            staged.append(("deleted", path))

    unstaged = []
    for path, sha in sorted(index.items()):
        full = repo.worktree / path
        if not full.is_file():
            unstaged.append(("deleted", path))
        elif blob_sha(full.read_bytes()) != sha:
            unstaged.append(("modified", path))

    tracked = set(index)
    untracked = [rel for rel in iter_worktree_files(repo) if rel not in tracked]

    if staged:
        print("\nChanges to be committed:")
        for label, path in staged:
            print(f"  {label}: {path}")
    if unstaged:
        print("\nChanges not staged for commit:")
        for label, path in unstaged:
            print(f"  {label}: {path}")
    if untracked:
        print("\nUntracked files:")
        for rel in untracked:
            print(f"  {rel}")
    if not (staged or unstaged or untracked):
        print("\nnothing to commit, working tree clean")


def cmd_log(args):
    repo = find_repo()
    sha = resolve_object(repo, args.commit)
    seen = set()
    while sha and sha not in seen:
        seen.add(sha)
        typ, data = read_object(repo, sha)
        if typ != "commit":
            raise GitError(f"{sha} is not a commit.")
        fields, message = parse_commit(data)
        author = fields.get("author", [""])[0]
        print(f"commit {sha}")
        if author:
            print(f"Author: {author.rsplit(' ', 2)[0]}")
            print(f"Date:   {format_identity_date(author)}")
        print()
        for line in message.splitlines():
            print(f"    {line}")
        print()
        sha = fields.get("parent", [None])[0]


def cmd_hash_object(args):
    repo = find_repo() if args.write else None
    if args.type != "blob":
        raise GitError("This simplified version supports hash-object for blobs only.")
    print(hash_file(args.path, repo, args.write))


def cmd_cat_file(args):
    repo = find_repo()
    sha = resolve_object(repo, args.object)
    typ, data = read_object(repo, sha)
    if typ != args.type:
        raise GitError(f"Object is {typ}, not {args.type}.")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def cmd_ls_files(args):
    repo = find_repo()
    index = load_index(repo)
    for path, sha in sorted(index.items()):
        print(f"{sha} {path}" if args.verbose else path)


def cmd_ls_tree(args):
    repo = find_repo()
    sha = resolve_object(repo, args.tree)
    typ, data = read_object(repo, sha)
    if typ == "commit":
        fields, _ = parse_commit(data)
        sha = fields["tree"][0]
    elif typ != "tree":
        raise GitError("The supplied object is not a tree or commit.")

    def display(tree_sha, prefix=""):
        for mode, name, child_sha in tree_entries(repo, tree_sha):
            is_tree = is_tree_mode(mode)
            path = f"{prefix}{name}"
            if args.recursive and is_tree:
                display(child_sha, path + "/")
                continue
            child_type = "tree" if is_tree else "blob"
            print(f"{mode.zfill(6)} {child_type} {child_sha}\t{path}")

    display(sha)


def cmd_rev_parse(args):
    repo = find_repo()
    sha = resolve_object(repo, args.name)
    if args.type:
        typ, _ = read_object(repo, sha)
        if typ != args.type:
            raise GitError(f"Expected {args.type}, found {typ}.")
    print(sha)


def cmd_show_ref(args):
    repo = find_repo()
    for base in ("heads", "tags", "remotes"):
        root = repo.gitdir / "refs" / base
        if root.exists():
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    name = path.relative_to(repo.gitdir).as_posix()
                    print(f"{path.read_text(encoding='utf-8').strip()} {name}")


def cmd_tag(args):
    repo = find_repo()
    if not args.name:
        root = repo.gitdir / "refs" / "tags"
        if root.exists():
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    print(path.relative_to(root).as_posix())
        return
    check_ref_name(args.name)
    if (repo.gitdir / "refs" / "tags" / args.name).exists():
        raise GitError(f"Tag already exists: {args.name}")
    sha = resolve_object(repo, args.object)
    write_ref(repo, f"refs/tags/{args.name}", sha)
    print(f"Created tag {args.name}")


def cmd_checkout(args):
    repo = find_repo()
    sha = resolve_object(repo, args.commit)
    typ, data = read_object(repo, sha)
    if typ == "commit":
        fields, _ = parse_commit(data)
        sha = fields["tree"][0]
    elif typ != "tree":
        raise GitError("Checkout requires a commit or tree.")
    destination = Path(args.path).resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise GitError("Checkout destination must be an empty directory.")
    destination.mkdir(parents=True, exist_ok=True)

    def extract(tree_sha, folder):
        for mode, name, child_sha in tree_entries(repo, tree_sha):
            if name in ("", ".", "..", ".git") or "/" in name or "\\" in name:
                raise GitError(f"Unsafe path in tree: {name!r}")
            child_type, child_data = read_object(repo, child_sha)
            target = folder / name
            if child_type == "tree":
                target.mkdir(parents=True, exist_ok=True)
                extract(child_sha, target)
            elif child_type == "blob":
                target.write_bytes(child_data)

    extract(sha, destination)
    print(f"Checked out {args.commit} into {destination}")


def cmd_check_ignore(args):
    repo = find_repo()
    patterns = load_ignore_patterns(repo)
    found = False
    for raw in args.paths:
        path, rel = to_relative(repo, raw)
        if rel and is_ignored(patterns, rel, path.is_dir()):
            print(raw)
            found = True
    return 0 if found else 1



# CLI


def build_parser():
    parser = argparse.ArgumentParser(
        prog="your-own-git",
        description="Your Own Git - a small Git-like version-control tool"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("init", help="Initialize a repository")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_init)

    p = commands.add_parser("add", help="Add file contents to the index")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_add)

    p = commands.add_parser("rm", help="Remove files from the index")
    p.add_argument("--cached", action="store_true", help="Keep working-tree files")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_rm)

    p = commands.add_parser("commit", help="Record changes")
    p.add_argument("-m", "--message", required=True)
    p.set_defaults(func=cmd_commit)

    p = commands.add_parser("status", help="Show working-tree status")
    p.set_defaults(func=cmd_status)

    p = commands.add_parser("log", help="Show commit history")
    p.add_argument("commit", nargs="?", default="HEAD")
    p.set_defaults(func=cmd_log)

    p = commands.add_parser("hash-object", help="Compute an object ID")
    p.add_argument("-t", "--type", default="blob", choices=["blob"])
    p.add_argument("-w", "--write", action="store_true")
    p.add_argument("path")
    p.set_defaults(func=cmd_hash_object)

    p = commands.add_parser("cat-file", help="Display object contents")
    p.add_argument("type", choices=["blob", "tree", "commit", "tag"])
    p.add_argument("object")
    p.set_defaults(func=cmd_cat_file)

    p = commands.add_parser("ls-files", help="List indexed files")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_ls_files)

    p = commands.add_parser("ls-tree", help="List tree contents")
    p.add_argument("-r", "--recursive", action="store_true")
    p.add_argument("tree")
    p.set_defaults(func=cmd_ls_tree)

    p = commands.add_parser("rev-parse", help="Resolve an object or reference")
    p.add_argument("--type", choices=["blob", "tree", "commit", "tag"])
    p.add_argument("name")
    p.set_defaults(func=cmd_rev_parse)

    p = commands.add_parser("show-ref", help="List references")
    p.set_defaults(func=cmd_show_ref)

    p = commands.add_parser("tag", help="List or create tags")
    p.add_argument("name", nargs="?")
    p.add_argument("object", nargs="?", default="HEAD")
    p.set_defaults(func=cmd_tag)

    p = commands.add_parser("checkout", help="Export a commit into an empty directory")
    p.add_argument("commit")
    p.add_argument("path")
    p.set_defaults(func=cmd_checkout)

    p = commands.add_parser("check-ignore", help="Check paths against .gitignore")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_check_ignore)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except (GitError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())