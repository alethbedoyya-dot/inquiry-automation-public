"""
OMS 待处理行分组：项目名优先；无项目名时按直发地址/梯号/寻源单号/系统询价号关联（并查集）。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

# 关联字段在 item 上可能出现的键（表头中文 + 程序内字段）
FIELD_DIRECT_ADDRESS = "direct_address"
FIELD_LADDER = "ladder"
FIELD_SOURCING = "sourcing"
FIELD_SYSTEM_INQUIRY = "system_inquiry"

_FIELD_KEY_ALIASES: Dict[str, Tuple[str, ...]] = {
    FIELD_DIRECT_ADDRESS: (
        "直发地址",
        "direct_address",
        "oms_direct_address",
    ),
    FIELD_LADDER: ("梯号", "ladder_no"),
    FIELD_SOURCING: ("寻源单号", "oms_sourcing_no", "sourcing_no", "Sour No"),
    FIELD_SYSTEM_INQUIRY: (
        "系统询价号",
        "系统询价价号",
        "系统查询价号",
        "oms_system_inquiry_no",
    ),
}


def _norm(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def field_value(item: dict, field_id: str) -> str:
    """从一行 OMS 数据取某一关联字段（去空白）。"""
    for key in _FIELD_KEY_ALIASES.get(field_id, ()):
        v = _norm(item.get(key))
        if v:
            return v
    return ""


def link_field_values(item: dict) -> Dict[str, str]:
    """返回四个关联字段的非空值。"""
    return {
        FIELD_DIRECT_ADDRESS: field_value(item, FIELD_DIRECT_ADDRESS),
        FIELD_LADDER: field_value(item, FIELD_LADDER),
        FIELD_SOURCING: field_value(item, FIELD_SOURCING),
        FIELD_SYSTEM_INQUIRY: field_value(item, FIELD_SYSTEM_INQUIRY),
    }


def project_name_of(item: dict) -> str:
    return _norm(item.get("项目名称") or item.get("project_name"))


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def rows_share_link_field(a: dict, b: dict) -> bool:
    """两行是否在四个关联字段中至少有一个完全相同（非空）。"""
    fa, fb = link_field_values(a), link_field_values(b)
    for key in fa:
        if fa[key] and fa[key] == fb[key]:
            return True
    return False


def assess_group_confidence(items: List[dict]) -> Tuple[bool, str]:
    """
    判断无项目名分组是否需人工确认。
    返回 (needs_review, reason)。
    """
    if len(items) < 2:
        return False, ""

    n = len(items)
    for i in range(n):
        for j in range(i + 1, n):
            if not rows_share_link_field(items[i], items[j]):
                return (
                    True,
                    "组内存在两行在直发地址/梯号/寻源单号/系统询价号上均无相同值"
                    "（仅靠其它行间接合并，请核对是否同一项目）",
                )

    # 过短的关联值易误合并
    short_vals: Set[str] = set()
    for item in items:
        for v in link_field_values(item).values():
            if v and len(v) <= 3:
                short_vals.add(v)
    if short_vals:
        return (
            True,
            f"关联字段取值过短，可能误合并: {', '.join(sorted(short_vals)[:5])}",
        )

    return False, "组内任意两行均至少共享一个关联字段"


def make_anonymous_group_key(items: List[dict]) -> str:
    """无项目名时生成 oms_data.json 分组键。"""
    sourcing = sorted({field_value(it, FIELD_SOURCING) for it in items if field_value(it, FIELD_SOURCING)})
    if len(sourcing) == 1:
        return f"寻源:{sourcing[0]}"

    addrs = sorted({field_value(it, FIELD_DIRECT_ADDRESS) for it in items if field_value(it, FIELD_DIRECT_ADDRESS)})
    if len(addrs) == 1:
        a = addrs[0]
        return f"地址:{a[:36]}{'…' if len(a) > 36 else ''}"

    ladders = sorted({field_value(it, FIELD_LADDER) for it in items if field_value(it, FIELD_LADDER)})
    if ladders:
        head = ladders[0][:24]
        suffix = f"等{len(ladders)}梯" if len(ladders) > 1 else ""
        return f"未命名_{head}{suffix}_{len(items)}行"

    sys_nos = sorted({field_value(it, FIELD_SYSTEM_INQUIRY) for it in items if field_value(it, FIELD_SYSTEM_INQUIRY)})
    if len(sys_nos) == 1:
        return f"询价:{sys_nos[0]}"

    return f"未命名组_{len(items)}行"


def group_oms_rows(data: List[dict]) -> Dict[str, List[dict]]:
    """
    分组规则：
    1. 有项目名称 → 按项目名（去空白）合并；
    2. 无项目名称 → 四个关联字段任一非空且值相同则并为一组（并查集传递合并）。
    """
    groups: Dict[str, List[dict]] = {}
    unnamed: List[dict] = []

    for item in data:
        name = project_name_of(item)
        if name:
            groups.setdefault(name, []).append(item)
        else:
            unnamed.append(item)

    if not unnamed:
        return groups

    n = len(unnamed)
    uf = _UnionFind(n)
    index_by_value: Dict[str, Dict[str, List[int]]] = {
        FIELD_DIRECT_ADDRESS: defaultdict(list),
        FIELD_LADDER: defaultdict(list),
        FIELD_SOURCING: defaultdict(list),
        FIELD_SYSTEM_INQUIRY: defaultdict(list),
    }

    for i, item in enumerate(unnamed):
        for fid, val in link_field_values(item).items():
            if val:
                index_by_value[fid][val].append(i)

    for fid in index_by_value:
        for indices in index_by_value[fid].values():
            if len(indices) < 2:
                continue
            root = indices[0]
            for j in indices[1:]:
                uf.union(root, j)

    clusters: Dict[int, List[dict]] = defaultdict(list)
    for i, item in enumerate(unnamed):
        clusters[uf.find(i)].append(item)

    for cluster in clusters.values():
        key = make_anonymous_group_key(cluster)
        # 避免与已有项目名键冲突
        if key in groups:
            base = key
            k = 2
            while f"{base}#{k}" in groups:
                k += 1
            key = f"{base}#{k}"

        needs_review, reason = assess_group_confidence(cluster)
        for it in cluster:
            it["group_needs_review"] = needs_review
            it["group_link_reason"] = reason
            it["group_key"] = key
        groups[key] = cluster

    return groups


def collect_group_match_needles(
    items: List[dict],
    project_name: str = "",
    *,
    min_len: int = 4,
) -> List[str]:
    """
    供 OMS 表格行勾选：项目名 + 组内四个字段的全部非空去重值（长度>=min_len）。
    """
    needles: List[str] = []
    seen: Set[str] = set()

    def add(v: str) -> None:
        v = _norm(v)
        if len(v) < min_len or v in seen:
            return
        seen.add(v)
        needles.append(v)

    pn = _norm(project_name)
    if pn:
        add(pn)
    for item in items:
        for v in link_field_values(item).values():
            add(v)
    return needles


def format_group_summary(items: List[dict], group_key: str = "") -> str:
    """打印给操作员的分组摘要。"""
    lines = [f"分组键: {group_key or make_anonymous_group_key(items)}", f"行数: {len(items)}"]
    for label, fid in (
        ("寻源单号", FIELD_SOURCING),
        ("直发地址", FIELD_DIRECT_ADDRESS),
        ("梯号", FIELD_LADDER),
        ("系统询价号", FIELD_SYSTEM_INQUIRY),
    ):
        vals = sorted({field_value(it, fid) for it in items if field_value(it, fid)})
        if vals:
            show = vals if len(vals) <= 6 else vals[:5] + [f"…共{len(vals)}个"]
            lines.append(f"  {label}: {', '.join(show)}")
    reason = (items[0].get("group_link_reason") if items else "") or ""
    if reason:
        lines.append(f"  说明: {reason}")
    return "\n".join(lines)


def parse_anonymous_group_key(group_key: str) -> Optional[str]:
    """从分组键解析寻源单号（寻源:XXX）。"""
    k = (group_key or "").strip()
    if k.startswith("寻源:"):
        return k[3:].strip()
    return None


def _unique_field_values(items: List[dict], field_id: str) -> List[str]:
    return sorted({field_value(it, field_id) for it in items if field_value(it, field_id)})


def resolve_group_oms_email_filter(
    project_name: str = "",
    group_key: str = "",
    items: Optional[List[dict]] = None,
) -> Optional[Tuple[str, str]]:
    """
    阶段一发邮件前 OMS 列漏斗：(列名, 筛选值)。

    与分组规则一致：
      - 有项目名称 → 项目名称；
      - 无项目名 → 寻源单号 / 直发地址 / 梯号 / 系统询价号（优先级同 make_anonymous_group_key）。

    调用方须先 apply 待处理 + Sourcing8ID=当前账号，再对本函数返回的列做漏斗搜索。
    """
    rows = list(items or [])
    pn = (project_name or "").strip()
    if not pn:
        for item in rows:
            pn = project_name_of(item)
            if pn:
                break
    if pn:
        return ("项目名称", pn)

    gk = (group_key or "").strip()
    if not gk and rows:
        gk = (rows[0].get("group_key") or "").strip()

    if gk.startswith("寻源:"):
        sn = gk[3:].strip()
        if sn:
            return ("寻源单号", sn)

    if gk.startswith("询价:"):
        sys_no = gk[3:].strip()
        if sys_no:
            return ("系统询价号", sys_no)

    # 分组键里的地址可能被截断，必须用行内完整值
    if gk.startswith("地址:"):
        addrs = _unique_field_values(rows, FIELD_DIRECT_ADDRESS)
        if len(addrs) == 1:
            return ("直发地址", addrs[0])

    # 与 make_anonymous_group_key 相同优先级
    sourcing = _unique_field_values(rows, FIELD_SOURCING)
    if len(sourcing) == 1:
        return ("寻源单号", sourcing[0])

    addrs = _unique_field_values(rows, FIELD_DIRECT_ADDRESS)
    if len(addrs) == 1:
        return ("直发地址", addrs[0])

    ladders = _unique_field_values(rows, FIELD_LADDER)
    if len(ladders) == 1:
        return ("梯号", ladders[0])

    sys_nos = _unique_field_values(rows, FIELD_SYSTEM_INQUIRY)
    if len(sys_nos) == 1:
        return ("系统询价号", sys_nos[0])

    return None
