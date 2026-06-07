"""Shell command semantics classifier.

每个 `run_shell` 命令在进入 sandbox 或 policy check 前，先被解析成
"命令链 → 管道段 → 主命令" 的结构，然后按主命令的角色分级。

Roles (按风险从高到低):
- destructive  : rm -rf, chmod, chown, dd, mkfs, fdisk, shutdown, reboot, ...
- network      : curl, wget, nc, telnet, ssh, scp, rsync (remote), ...
- install      : pip install, npm install -g, gem install, cargo install, ...
- vcs          : git, hg, svn — 可能改变 repo 状态
- build        : make, cmake, ninja, cargo build, go build, ...
- test         : pytest, go test, cargo test, npm test, ...
- process      : ps, top, kill, pkill, htop, systemctl, service, ...
- environment  : export, source, set, unset, alias, ...
- read_search  : cat, grep, find, ls, head, tail, rg, less — 策略应该拦截
- info         : echo, printf, date, whoami, pwd, which, man, ...
- other        : 无法归类

为什么存在:
旧的 `SHELL_SEARCH_RE` 只靠一个正则来判断 "是不是在偷偷做搜索"。
真实 shell 命令可以包含 `;`, `&&`, `||`, `|`, 重定向等, 正则很难准确覆盖。
这里把解析和分级分开: 先拆分出每个逻辑段的"主命令", 再逐段分角色,
这样 policy 层拿到的是结构化结论而不是模糊的正则匹配。
"""

import re
import shlex
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import List

# ── 角色常量 ──────────────────────────────────────────────────

# 破坏性命令 — 不可逆地删除/修改系统状态
ROLE_DESTRUCTIVE = "destructive"
# 网络通信 — 可能泄露数据或下载不受信代码
ROLE_NETWORK = "network"
# 包安装 — 在系统/用户级别安装软件
ROLE_INSTALL = "install"
# 版本控制 — 修改 git 历史
ROLE_VCS = "vcs"
# 构建 — 编译/链接
ROLE_BUILD = "build"
# 测试运行
ROLE_TEST = "test"
# 进程管理 — 查看/操作运行中进程
ROLE_PROCESS = "process"
# 环境变量修改
ROLE_ENVIRONMENT = "environment"
# 读/搜索 — 应该用 read_file / search / list_files 替代
ROLE_READ_SEARCH = "read_search"
# 纯信息 — echo, date, whoami 等无副作用命令
ROLE_INFO = "info"
# 无法归类
ROLE_OTHER = "other"

# 按风险从高到低排序（policy 层取最高风险角色）
ROLE_RISK_ORDER = [
    ROLE_DESTRUCTIVE,
    ROLE_NETWORK,
    ROLE_INSTALL,
    ROLE_VCS,
    ROLE_BUILD,
    ROLE_TEST,
    ROLE_PROCESS,
    ROLE_ENVIRONMENT,
    ROLE_READ_SEARCH,
    ROLE_INFO,
    ROLE_OTHER,
]

ROLE_RISK_LEVEL = {role: i for i, role in enumerate(ROLE_RISK_ORDER)}

# ── 命令分类表 ────────────────────────────────────────────────

# 注意：带子命令的（如 `git clone`, `pip install`）需要看第二个 token，
# 在 _resolve_role 里特殊处理。
_COMMAND_ROLE_MAP = {
    # destructive
    "rm": ROLE_DESTRUCTIVE,
    "rmdir": ROLE_DESTRUCTIVE,
    "chmod": ROLE_DESTRUCTIVE,
    "chown": ROLE_DESTRUCTIVE,
    "chgrp": ROLE_DESTRUCTIVE,
    "dd": ROLE_DESTRUCTIVE,
    "mkfs": ROLE_DESTRUCTIVE,
    "fdisk": ROLE_DESTRUCTIVE,
    "shutdown": ROLE_DESTRUCTIVE,
    "reboot": ROLE_DESTRUCTIVE,
    "halt": ROLE_DESTRUCTIVE,
    "poweroff": ROLE_DESTRUCTIVE,
    "truncate": ROLE_DESTRUCTIVE,
    "mv": ROLE_DESTRUCTIVE,
    "unlink": ROLE_DESTRUCTIVE,
    # network
    "curl": ROLE_NETWORK,
    "wget": ROLE_NETWORK,
    "nc": ROLE_NETWORK,
    "ncat": ROLE_NETWORK,
    "telnet": ROLE_NETWORK,
    "ssh": ROLE_NETWORK,
    "scp": ROLE_NETWORK,
    "sftp": ROLE_NETWORK,
    "rsync": ROLE_NETWORK,
    "ftp": ROLE_NETWORK,
    "tftp": ROLE_NETWORK,
    "socat": ROLE_NETWORK,
    # install
    "pip": ROLE_INSTALL,
    "pip3": ROLE_INSTALL,
    "npm": ROLE_INSTALL,
    "npx": ROLE_INSTALL,
    "yarn": ROLE_INSTALL,
    "pnpm": ROLE_INSTALL,
    "gem": ROLE_INSTALL,
    "cargo": ROLE_INSTALL,
    "go": ROLE_INSTALL,
    "apt": ROLE_INSTALL,
    "apt-get": ROLE_INSTALL,
    "dnf": ROLE_INSTALL,
    "yum": ROLE_INSTALL,
    "pacman": ROLE_INSTALL,
    "brew": ROLE_INSTALL,
    "zypper": ROLE_INSTALL,
    "snap": ROLE_INSTALL,
    "flatpak": ROLE_INSTALL,
    "dpkg": ROLE_INSTALL,
    "rpm": ROLE_INSTALL,
    # vcs
    "git": ROLE_VCS,
    "hg": ROLE_VCS,
    "svn": ROLE_VCS,
    "bzr": ROLE_VCS,
    # build
    "make": ROLE_BUILD,
    "cmake": ROLE_BUILD,
    "ninja": ROLE_BUILD,
    "meson": ROLE_BUILD,
    "bazel": ROLE_BUILD,
    "buck2": ROLE_BUILD,
    "mvn": ROLE_BUILD,
    "gradle": ROLE_BUILD,
    "sbt": ROLE_BUILD,
    "ant": ROLE_BUILD,
    # test
    "pytest": ROLE_TEST,
    "tox": ROLE_TEST,
    "nox": ROLE_TEST,
    "coverage": ROLE_TEST,
    "jest": ROLE_TEST,
    "mocha": ROLE_TEST,
    "vitest": ROLE_TEST,
    # process
    "ps": ROLE_PROCESS,
    "top": ROLE_PROCESS,
    "htop": ROLE_PROCESS,
    "kill": ROLE_PROCESS,
    "pkill": ROLE_PROCESS,
    "killall": ROLE_PROCESS,
    "systemctl": ROLE_PROCESS,
    "service": ROLE_PROCESS,
    "supervisorctl": ROLE_PROCESS,
    "pgrep": ROLE_PROCESS,
    "pidof": ROLE_PROCESS,
    "nice": ROLE_PROCESS,
    "renice": ROLE_PROCESS,
    "nohup": ROLE_PROCESS,
    "bg": ROLE_PROCESS,
    "fg": ROLE_PROCESS,
    "jobs": ROLE_PROCESS,
    "disown": ROLE_PROCESS,
    # environment
    "export": ROLE_ENVIRONMENT,
    "source": ROLE_ENVIRONMENT,
    "set": ROLE_ENVIRONMENT,
    "unset": ROLE_ENVIRONMENT,
    "alias": ROLE_ENVIRONMENT,
    "unalias": ROLE_ENVIRONMENT,
    "env": ROLE_ENVIRONMENT,
    "printenv": ROLE_ENVIRONMENT,
    # read_search — 用工具替代
    "cat": ROLE_READ_SEARCH,
    "grep": ROLE_READ_SEARCH,
    "egrep": ROLE_READ_SEARCH,
    "fgrep": ROLE_READ_SEARCH,
    "rg": ROLE_READ_SEARCH,
    "find": ROLE_READ_SEARCH,
    "ls": ROLE_READ_SEARCH,
    "dir": ROLE_READ_SEARCH,
    "head": ROLE_READ_SEARCH,
    "tail": ROLE_READ_SEARCH,
    "less": ROLE_READ_SEARCH,
    "more": ROLE_READ_SEARCH,
    "wc": ROLE_READ_SEARCH,
    "sort": ROLE_READ_SEARCH,
    "uniq": ROLE_READ_SEARCH,
    "cut": ROLE_READ_SEARCH,
    "awk": ROLE_READ_SEARCH,
    "sed": ROLE_READ_SEARCH,
    "tr": ROLE_READ_SEARCH,
    "diff": ROLE_READ_SEARCH,
    "cmp": ROLE_READ_SEARCH,
    "comm": ROLE_READ_SEARCH,
    "file": ROLE_READ_SEARCH,
    "stat": ROLE_READ_SEARCH,
    "du": ROLE_READ_SEARCH,
    "df": ROLE_READ_SEARCH,
    "tree": ROLE_READ_SEARCH,
    "fd": ROLE_READ_SEARCH,
    "fdfind": ROLE_READ_SEARCH,
    "locate": ROLE_READ_SEARCH,
    "which": ROLE_READ_SEARCH,
    "whereis": ROLE_READ_SEARCH,
    "type": ROLE_READ_SEARCH,
    "readlink": ROLE_READ_SEARCH,
    "realpath": ROLE_READ_SEARCH,
    "xxd": ROLE_READ_SEARCH,
    "hexdump": ROLE_READ_SEARCH,
    "od": ROLE_READ_SEARCH,
    "strings": ROLE_READ_SEARCH,
    "nl": ROLE_READ_SEARCH,
    "tee": ROLE_READ_SEARCH,
    "xargs": ROLE_READ_SEARCH,
    # info — 无副作用
    "echo": ROLE_INFO,
    "printf": ROLE_INFO,
    "date": ROLE_INFO,
    "whoami": ROLE_INFO,
    "who": ROLE_INFO,
    "pwd": ROLE_INFO,
    "hostname": ROLE_INFO,
    "uname": ROLE_INFO,
    "uptime": ROLE_INFO,
    "id": ROLE_INFO,
    "groups": ROLE_INFO,
    "man": ROLE_INFO,
    "help": ROLE_INFO,
    "true": ROLE_INFO,
    "false": ROLE_INFO,
    "yes": ROLE_INFO,
    "sleep": ROLE_INFO,
    "wait": ROLE_INFO,
    "basename": ROLE_INFO,
    "dirname": ROLE_INFO,
    "test_cmd": ROLE_INFO,
    "[": ROLE_INFO,
    "expr": ROLE_INFO,
}

# 这些命令用来看/读/搜索 workspace 内容，不应绕到 shell
# policy 层应拦截并提示用 read_file / search / list_files
_POLICY_BLOCKED_LEADS = frozenset({
    "cat", "grep", "egrep", "fgrep", "rg", "find", "ls", "dir",
    "head", "tail", "less", "more", "wc", "sort", "uniq",
    "file", "stat", "tree", "fd", "fdfind", "locate",
    "which", "whereis", "type", "readlink", "realpath",
    "xxd", "hexdump", "od", "strings", "nl",
})

# 这些子命令/模式让原本安全的命令变成潜在的包安装
_INSTALL_SUBCOMMANDS = frozenset({
    "install", "add", "global",
})

_INSTALL_SUBCOMMAND_PAIRS = frozenset({
    ("pip", "install"),
    ("pip3", "install"),
    ("pip", "uninstall"),
    ("pip3", "uninstall"),
    ("npm", "install"),
    ("npm", "add"),
    ("npm", "i"),
    ("npm", "update"),
    ("npm", "uninstall"),
    ("yarn", "add"),
    ("yarn", "global"),
    ("pnpm", "add"),
    ("pnpm", "install"),
    ("gem", "install"),
    ("cargo", "install"),
    ("go", "install"),
    ("go", "get"),
    ("apt", "install"),
    ("apt", "remove"),
    ("apt-get", "install"),
    ("apt-get", "remove"),
    ("dnf", "install"),
    ("dnf", "remove"),
    ("yum", "install"),
    ("pacman", "-S"),
    ("pacman", "-R"),
    ("brew", "install"),
    ("brew", "uninstall"),
    ("snap", "install"),
})


# ── 解析辅助 ──────────────────────────────────────────────────

def _tokenize(command: str) -> List[str]:
    """安全地拆出命令 token，失败时退化为简单 split。"""
    try:
        return shlex.split(str(command))
    except ValueError:
        return str(command).split()


def _command_chain_segments(command: str) -> List[str]:
    r"""把命令按链分隔符 `;`, `&&`, `||` 拆成独立逻辑段。

    >>> _command_chain_segments("echo a && cat file")
    ['echo a', 'cat file']
    """
    # 用正则按 ; && || 拆分，但要保留管道 | 不动
    parts = re.split(r"(?:;|&&|\|\|)\s*", str(command))
    return [p.strip() for p in parts if p.strip()]


def _pipeline_lead_words(segment: str) -> List[str]:
    """取一个段在管道 | 之前的主命令部分。

    >>> _pipeline_lead_words("pip install foo | tail -5")
    ['pip', 'install', 'foo']
    """
    # 先拆管道，取第一段
    pipe_parts = segment.split("|")
    lead = pipe_parts[0].strip()
    # 去重定向
    lead = re.sub(r">>?\s*\S+", "", lead)
    lead = re.sub(r"<<?\s*\S+", "", lead)
    lead = re.sub(r"2?>>?&?\s*\S+", "", lead)  # 2>&1, &>
    lead = re.sub(r"<\S+", "", lead)
    return _tokenize(lead)


def _resolve_role(tokens: List[str]) -> str:
    """根据 token 列表决定命令角色，处理子命令覆盖。"""
    if not tokens:
        return ROLE_OTHER

    main = tokens[0]
    # 处理 `sudo cmd` — 跳过 sudo
    if main in ("sudo", "doas", "pkexec"):
        return _resolve_role(tokens[1:])

    # 处理 `python -c`, `python script.py`, `python -m pytest`
    if main in ("python", "python3", "python2", "py"):
        if len(tokens) >= 2:
            if tokens[1] == "-m":
                return _resolve_role(tokens[2:])
            if tokens[1] == "-c":
                return ROLE_OTHER
            # `python script.py` → test if script looks like a test runner
            script = tokens[1]
            if any(kw in script for kw in ("test", "check", "verify", "lint")):
                return ROLE_TEST
        return ROLE_OTHER

    # 处理 `uv run ...` — 还原真实命令
    if main == "uv" and len(tokens) >= 2 and tokens[1] == "run":
        return _resolve_role(tokens[2:])

    # 处理子命令覆盖
    if len(tokens) >= 2:
        pair = (main, tokens[1])
        if pair in _INSTALL_SUBCOMMAND_PAIRS:
            return ROLE_INSTALL
        # git clone/push/pull/fetch → network
        if main == "git" and tokens[1] in ("clone", "push", "pull", "fetch", "remote"):
            return ROLE_NETWORK
        # git 其他子命令 → vcs
        if main == "git":
            return ROLE_VCS
        # cargo build/check → build; cargo test → test
        if main == "cargo" and tokens[1] in ("build", "check", "clippy", "fmt"):
            return ROLE_BUILD
        if main == "cargo" and tokens[1] in ("test", "bench"):
            return ROLE_TEST
        # go build → build; go test → test; go run → build
        if main == "go" and tokens[1] in ("build", "run", "vet", "fmt"):
            return ROLE_BUILD
        if main == "go" and tokens[1] == "test":
            return ROLE_TEST
        # npm run test / npm test → test; npm run build → build
        if main == "npm" and tokens[1] == "test":
            return ROLE_TEST
        if main == "npm" and tokens[1] == "run" and len(tokens) >= 3:
            if tokens[2] in ("build", "compile", "bundle"):
                return ROLE_BUILD
            if tokens[2] in ("test", "check", "lint"):
                return ROLE_TEST
        # pip / pip3 without install → could be `pip list`, `pip freeze` (info)
        if main in ("pip", "pip3") and tokens[1] not in _INSTALL_SUBCOMMANDS:
            return ROLE_INFO

    return _COMMAND_ROLE_MAP.get(main, ROLE_OTHER)


# ── 公开 API ──────────────────────────────────────────────────


@dataclass
class CommandClassification:
    """一条 shell 命令的完整语义分类结果。

    Attributes:
        command: 原始命令字符串
        highest_role: 所有段中风险最高的角色
        highest_risk_level: 风险等级（0 = 最高风险）
        segments: 每个逻辑段（; && || 分隔）的分类结果
        is_destructive: 是否包含破坏性操作
        is_network: 是否涉及网络通信
        is_install: 是否安装包
        has_read_search_lead: 是否有段的主命令是读/搜索（policy 应拦截）
    """

    command: str = ""
    highest_role: str = ROLE_OTHER
    highest_risk_level: int = len(ROLE_RISK_ORDER)
    segments: List[dict] = field(default_factory=list)
    is_destructive: bool = False
    is_network: bool = False
    is_install: bool = False
    has_read_search_lead: bool = False


def classify_command(command: str) -> CommandClassification:
    """解析一条 shell 命令串并返回完整分类。

    用法::

        c = classify_command("pip install requests && echo done")
        assert c.is_install
        assert c.highest_role == "install"

        c = classify_command("cat README.md | grep TODO")
        assert c.has_read_search_lead  # policy 应拦截

        c = classify_command("python -m pytest -q | tail -20")
        assert c.segments[0]["role"] == "test"
        assert not c.has_read_search_lead  # tail 在管道后，不算 lead
    """
    raw = str(command or "")
    segments = _command_chain_segments(raw)
    if not segments:
        return CommandClassification(command=raw)

    classified_segments = []
    roles_seen = set()

    for seg in segments:
        lead_tokens = _pipeline_lead_words(seg)
        role = _resolve_role(lead_tokens)
        roles_seen.add(role)
        classified_segments.append({
            "segment": seg,
            "lead_tokens": lead_tokens,
            "role": role,
            "risk_level": ROLE_RISK_LEVEL.get(role, len(ROLE_RISK_ORDER)),
        })

    highest_role = ROLE_OTHER
    highest_risk_level = len(ROLE_RISK_ORDER)
    for role in ROLE_RISK_ORDER:
        if role in roles_seen:
            highest_role = role
            highest_risk_level = ROLE_RISK_LEVEL[role]
            break

    has_read_search_lead = any(
        seg["lead_tokens"]
        and seg["lead_tokens"][0] in _POLICY_BLOCKED_LEADS
        for seg in classified_segments
    )

    return CommandClassification(
        command=raw,
        highest_role=highest_role,
        highest_risk_level=highest_risk_level,
        segments=classified_segments,
        is_destructive=ROLE_DESTRUCTIVE in roles_seen,
        is_network=ROLE_NETWORK in roles_seen,
        is_install=ROLE_INSTALL in roles_seen,
        has_read_search_lead=has_read_search_lead,
    )


def command_is_excluded(command, patterns):
    """检查命令是否匹配 exclusion 列表（fnmatch 模式）。

    保留向后兼容：SandboxRunner 仍通过此函数判断豁免。"""
    command = str(command or "").strip()
    return any(fnmatch(command, str(pattern)) for pattern in patterns or ())


# ── 便捷判断函数 ──────────────────────────────────────────────


def command_is_destructive(command: str) -> bool:
    """快速判断命令是否包含破坏性操作。"""
    return classify_command(command).is_destructive


def command_is_network(command: str) -> bool:
    """快速判断命令是否涉及网络。"""
    return classify_command(command).is_network


def command_has_read_search_lead(command: str) -> bool:
    """快速判断是否有段的主命令是读/搜索（应用工具替代）。"""
    return classify_command(command).has_read_search_lead


def command_highest_role(command: str) -> str:
    """返回命令的最高风险角色名。"""
    return classify_command(command).highest_role
