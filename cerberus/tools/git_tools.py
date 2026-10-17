"""Git, GitHub and VS Code tools - so Cerberus can pull, commit, push and open your projects.

 * Git runs through the `git` program on your PC (install: `winget install --id Git.Git -e`).
 * Your GitHub token (set in the Grimoire tab) is passed to git ONLY for github.com and never printed.
 * `git_push` and anything that creates things on GitHub are risk="publish": they always ask first.
 * Before committing, files that look like secrets (.env, keys, settings.json) are un-staged and the commit is stopped.
"""
from __future__ import annotations

import base64
import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import requests

from ..config import get_workspace, load_settings
from ..safety import safe_path
from .base import tool
from .shell import check_shim_args

GIT_MISSING = "Git is not installed. Install it with:  winget install --id Git.Git -e   (then restart Cerberus)."
SECRET_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.pfx", "settings.json", "*.db", "audit.log",
                   "*secret*", "*credentials*"]
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


# ---------------------------------------------------------------- helpers
def _token() -> str:
    return (load_settings().get("github") or {}).get("token", "").strip()


def _scrub(text: str) -> str:
    tok = _token()
    if tok:
        text = text.replace(tok, "***").replace(base64.b64encode(f"x-access-token:{tok}".encode()).decode(), "***")
    return text


def _git(args: list[str], repo: str = ".", network: bool = False, identity: bool = False, timeout: int = 120) -> str:
    exe = shutil.which("git")
    if not exe:
        return GIT_MISSING
    cwd = safe_path(repo)
    cwd.mkdir(parents=True, exist_ok=True)
    cfg: list[str] = []
    gh = load_settings().get("github") or {}
    if network and _token():
        b64 = base64.b64encode(f"x-access-token:{_token()}".encode()).decode()
        cfg += ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {b64}"]
    if identity:
        if gh.get("git_name"):
            cfg += ["-c", f"user.name={gh['git_name']}"]
        if gh.get("git_email"):
            cfg += ["-c", f"user.email={gh['git_email']}"]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang waiting for a password prompt
    try:
        proc = subprocess.run([exe, *cfg, *args], cwd=str(cwd), capture_output=True, text=True, errors="replace",
                              timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"git {args[0]} timed out after {timeout}s."
    out = _scrub((proc.stdout or "") + (proc.stderr or "")).strip()
    return out if proc.returncode == 0 else f"git {args[0]} failed (exit {proc.returncode}):\n{out}"


# ---------------------------------------------------------------- git tools
@tool("git_status", "Git", "Show changed files in a repository folder.", {"repo": "folder in workspace, default '.' (optional)"})
def git_status(ctx, repo: str = ".") -> str:
    return _git(["status", "--short", "--branch"], repo) or "Clean."


@tool("git_diff", "Git", "Show uncommitted changes.", {"repo": "folder (optional)"})
def git_diff(ctx, repo: str = ".") -> str:
    return _git(["diff", "--stat", "--patch", "--no-color"], repo) or "No changes."


@tool("git_log", "Git", "Show recent commits.", {"repo": "folder (optional)", "count": "how many, default 10 (optional)"})
def git_log(ctx, repo: str = ".", count: int = 10) -> str:
    return _git(["log", f"-{max(1, min(int(count), 50))}", "--oneline", "--decorate"], repo)


@tool("git_init", "Git", "Create a new git repository in a workspace folder.", {"repo": "folder (optional)"}, risk="write")
def git_init(ctx, repo: str = ".") -> str:
    return _git(["init", "-b", "main"], repo)


@tool("git_clone", "Git", "Clone a repository from GitHub into the workspace.",
      {"url": "https URL or owner/name", "folder": "target folder name (optional)"}, risk="write")
def git_clone(ctx, url: str, folder: str = "") -> str:
    if REPO_RE.match(url):
        url = f"https://github.com/{url}.git"
    if not url.startswith(("https://", "git@")):
        raise ValueError("Only https:// or git@ URLs are allowed.")
    target = folder or re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1])
    if target.startswith("-") or url.startswith("-"):
        raise ValueError("bad folder name")
    safe_path(target)  # sandbox check
    return _git(["clone", "--", url, target], ".", network=True, timeout=300)


@tool("git_pull", "Git", "Pull the latest changes from the remote.", {"repo": "folder (optional)"}, risk="write")
def git_pull(ctx, repo: str = ".") -> str:
    return _git(["pull", "--ff-only"], repo, network=True, timeout=180)


@tool("git_checkout", "Git", "Switch to a branch (optionally creating it).",
      {"branch": "branch name", "create": "true to create (optional)", "repo": "folder (optional)"}, risk="write")
def git_checkout(ctx, branch: str, create: bool = False, repo: str = ".") -> str:
    if not re.match(r"^[\w./-]+$", branch) or branch.startswith("-"):
        raise ValueError("bad branch name")
    return _git(["checkout", *(["-b"] if create else []), branch], repo)


@tool("git_remote", "Git", "Set the 'origin' remote URL of a repository.",
      {"url": "https URL", "repo": "folder (optional)"}, risk="write")
def git_remote(ctx, url: str, repo: str = ".") -> str:
    if not url.startswith("https://"):
        raise ValueError("Only https:// remotes are allowed.")
    existing = _git(["remote"], repo)
    return _git(["remote", "set-url" if "origin" in existing.split() else "add", "origin", url], repo) or "origin set."


@tool("git_commit", "Git", "Stage all changes and commit them (stops if a file looks like a secret).",
      {"message": "commit message", "repo": "folder (optional)"}, risk="write")
def git_commit(ctx, message: str, repo: str = ".") -> str:
    if not message.strip():
        raise ValueError("commit message is empty")
    add = _git(["add", "-A"], repo)
    if add.startswith(("git add failed", "Git is not")):
        return add
    staged = _git(["diff", "--cached", "--name-only"], repo).splitlines()
    risky = [f for f in staged if any(fnmatch.fnmatch(Path(f).name.lower(), pat) for pat in SECRET_PATTERNS)]
    if risky:
        _git(["reset", "-q"], repo)
        return ("STOPPED: these files look like they contain secrets and were un-staged: "
                + ", ".join(risky) + ". Add them to .gitignore, then try again.")
    return _git(["commit", "-m", message], repo, identity=True)


@tool("git_push", "Git", "Push commits to the remote on GitHub.",
      {"repo": "folder (optional)", "branch": "branch (optional; default current)"}, risk="publish")
def git_push(ctx, repo: str = ".", branch: str = "") -> str:
    if branch and (not re.match(r"^[\w./-]+$", branch) or branch.startswith("-")):
        raise ValueError("bad branch name")
    args = ["push", "-u", "origin"] + ([branch] if branch else ["HEAD"])
    return _git(args, repo, network=True, timeout=300)


# ---------------------------------------------------------------- GitHub API
def _gh(method: str, path: str, json: Optional[dict] = None, params: Optional[dict] = None):
    if not _token():
        raise ValueError("No GitHub token yet. Add a Personal Access Token in the Grimoire tab.")
    r = requests.request(method, "https://api.github.com" + path, json=json, params=params, timeout=20,
                         headers={"Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json",
                                  "X-GitHub-Api-Version": "2022-11-28"})
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub {r.status_code}: {r.json().get('message', r.text[:200]) if r.content else ''}")
    return r.json() if r.content else {}


def _repo_arg(repo: str) -> str:
    if not REPO_RE.match(repo):
        raise ValueError("repo must look like 'owner/name'")
    return repo


@tool("github_whoami", "GitHub", "Check which GitHub account the saved token belongs to.")
def github_whoami(ctx) -> str:
    u = _gh("GET", "/user")
    return f"Logged in as {u['login']} ({u.get('name') or 'no display name'})."


@tool("github_list_repos", "GitHub", "List the user's GitHub repositories.", {"limit": "default 30 (optional)"})
def github_list_repos(ctx, limit: int = 30) -> str:
    repos = _gh("GET", "/user/repos", params={"per_page": min(int(limit), 100), "sort": "updated"})
    return "\n".join(f"{r['full_name']}{' (private)' if r['private'] else ''} - {r.get('description') or ''}"
                     for r in repos) or "No repositories."


@tool("github_get_file", "GitHub", "Read a file from a GitHub repository.",
      {"repo": "owner/name", "path": "file path in the repo", "ref": "branch (optional)"})
def github_get_file(ctx, repo: str, path: str, ref: str = "") -> str:
    data = _gh("GET", f"/repos/{_repo_arg(repo)}/contents/{path.lstrip('/')}", params={"ref": ref} if ref else None)
    if isinstance(data, list):
        return "\n".join(f"{i['type']:<4} {i['path']}" for i in data)
    return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")


@tool("github_list_issues", "GitHub", "List open issues of a repository.", {"repo": "owner/name"})
def github_list_issues(ctx, repo: str) -> str:
    issues = _gh("GET", f"/repos/{_repo_arg(repo)}/issues", params={"state": "open", "per_page": 20})
    return "\n".join(f"#{i['number']} {i['title']} ({i['html_url']})" for i in issues if "pull_request" not in i) \
        or "No open issues."


@tool("github_create_issue", "GitHub", "Open a new issue on a repository.",
      {"repo": "owner/name", "title": "issue title", "body": "issue text (optional)"}, risk="publish")
def github_create_issue(ctx, repo: str, title: str, body: str = "") -> str:
    i = _gh("POST", f"/repos/{_repo_arg(repo)}/issues", json={"title": title, "body": body})
    return f"Created issue #{i['number']}: {i['html_url']}"


@tool("github_create_repo", "GitHub", "Create a new (private by default) repository on GitHub.",
      {"name": "repository name", "description": "(optional)", "private": "true/false, default true (optional)"},
      risk="publish")
def github_create_repo(ctx, name: str, description: str = "", private: bool = True) -> str:
    if not re.match(r"^[A-Za-z0-9_.-]+$", name):
        raise ValueError("bad repository name")
    r = _gh("POST", "/user/repos", json={"name": name, "description": description, "private": bool(private)})
    return f"Created {r['full_name']}: {r['html_url']}  (clone url: {r['clone_url']})"


# ---------------------------------------------------------------- VS Code / Visual Studio
def find_editor() -> Optional[str]:
    cmd = load_settings().get("vscode_command") or "code"
    found = shutil.which(cmd)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    for guess in (Path(local) / "Programs/Microsoft VS Code/bin/code.cmd",
                  Path("C:/Program Files/Microsoft VS Code/bin/code.cmd")):
        if guess.exists():
            return str(guess)
    return None


@tool("vscode_open", "VS Code", "Open a file or folder from the workspace in VS Code (or Visual Studio if configured).",
      {"path": "file or folder in workspace, default '.' (optional)", "line": "line number (optional)"}, risk="write")
def vscode_open(ctx, path: str = ".", line: int = 0) -> str:
    exe = find_editor()
    if not exe:
        return "VS Code was not found. Install it, or set the editor command in the Grimoire tab."
    target = safe_path(path)
    if Path(exe).stem.lower() == "devenv":
        argv = [exe, "/Edit" if target.is_file() else "", str(target)]
        argv = [a for a in argv if a]
    else:
        argv = [exe, "--goto", f"{target}:{int(line)}"] if (line and target.is_file()) else [exe, str(target)]
    if exe.lower().endswith((".cmd", ".bat")):
        check_shim_args(argv[1:])
    subprocess.Popen(argv, creationflags=0x08000000 if os.name == "nt" else 0,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Opened {target.relative_to(get_workspace()).as_posix() or '.'} in the editor."
