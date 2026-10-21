"""Network measures from an edge list (method-layer §4.1): degree, betweenness, closeness, Freeman
centralization, optionally per year, and Gould–Fernandez brokerage roles when node groups are given.

The measure functions below are copied verbatim (inspect.getsource) into the run's `code`, so the
script a reader re-runs is the one that produced the numbers."""
from __future__ import annotations

import inspect
from typing import Any

import networkx as nx
import pandas as pd

from .stats import Outcome, StatsError, header, listwise, new_figure, table

ROLES = ("coordinator", "itinerant", "gatekeeper", "representative", "liaison")
ROLE_LABELS = {"coordinator": "协调者 coordinator", "itinerant": "巡回者 itinerant", "gatekeeper": "守门人 gatekeeper",
               "representative": "代表 representative", "liaison": "联络人 liaison"}
MAX_NODES = 5000


def node_id(v) -> str:
    """Node label as text; 3.0 read from a float column becomes "3"."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def build_graph(edges: pd.DataFrame, source: str, target: str, weight: str | None = None, directed: bool = False):
    G = nx.DiGraph() if directed else nx.Graph()
    ws = edges[weight].astype(float).tolist() if weight else [1.0] * len(edges)
    for a, b, w in zip(edges[source].tolist(), edges[target].tolist(), ws):
        a, b = node_id(a), node_id(b)
        if a == b:
            continue
        if G.has_edge(a, b):
            G[a][b]["weight"] += w
        else:
            G.add_edge(a, b, weight=w)
    return G


def degree_centralization(G) -> float | None:
    """Freeman degree centralization, 1 for a star, 0 when every node has the same degree
    (undirected; for directed graphs total degree is used against the same bound)."""
    n = G.number_of_nodes()
    if n < 3:
        return None
    deg = dict(G.degree())
    mx = max(deg.values())
    return sum(mx - d for d in deg.values()) / ((n - 1) * (n - 2))


def betweenness_centralization(G, betweenness: dict | None = None) -> float | None:
    """Freeman betweenness centralization on normalized betweenness: sum(max - b_i) / (n - 1)."""
    n = G.number_of_nodes()
    if n < 3:
        return None
    b = betweenness if betweenness is not None else nx.betweenness_centrality(G, normalized=True)
    mx = max(b.values())
    return sum(mx - v for v in b.values()) / (n - 1)


def brokerage(G, group: dict) -> dict:
    """Gould–Fernandez brokerage counts per broker node. For every two-path a → b → c with a != c
    and no tie a → c, broker b's role follows the groups of a, b, c. An undirected tie counts in both
    directions, so in an undirected network gatekeeper and representative counts coincide.
    Nodes without a group are skipped."""
    D = G if G.is_directed() else G.to_directed()
    roles = ("coordinator", "itinerant", "gatekeeper", "representative", "liaison")
    out = {b: dict.fromkeys(roles, 0) for b in D.nodes if b in group}
    for b in out:
        gb = group[b]
        for a in D.predecessors(b):
            if a not in group:
                continue
            for c in D.successors(b):
                if c == a or c not in group or D.has_edge(a, c):
                    continue
                ga, gc = group[a], group[c]
                if ga == gb == gc:
                    role = "coordinator"
                elif ga == gc != gb:
                    role = "itinerant"
                elif ga != gb and gb == gc:
                    role = "gatekeeper"
                elif ga == gb and gb != gc:
                    role = "representative"
                else:
                    role = "liaison"
                out[b][role] += 1
    return out


def measure(G) -> tuple[dict, list]:
    """Graph-level measures and per-node rows (degree, betweenness, closeness)."""
    btw = nx.betweenness_centrality(G, normalized=True, weight=None)
    clo = nx.closeness_centrality(G)
    deg = dict(G.degree())
    graph = {"nodes": G.number_of_nodes(), "edges": G.number_of_edges(), "density": nx.density(G),
             "degree_centralization": degree_centralization(G),
             "betweenness_centralization": betweenness_centralization(G, btw),
             "components": (nx.number_weakly_connected_components(G) if G.is_directed() else nx.number_connected_components(G))}
    nodes = sorted(([node, deg[node], btw[node], clo[node]] for node in G.nodes), key=lambda r: (-r[2], -r[1], r[0]))
    return graph, nodes


def node_groups(data: pd.DataFrame, source: str, target: str, group: str | None, target_group: str | None) -> dict:
    groups: dict[str, str] = {}
    if group:
        for a, g in zip(data[source].tolist(), data[group].tolist()):
            if pd.notna(g):
                groups.setdefault(node_id(a), node_id(g))
    if target_group:
        for c, g in zip(data[target].tolist(), data[target_group].tolist()):
            if pd.notna(g):
                groups.setdefault(node_id(c), node_id(g))
    return groups


def _period_label(v: Any) -> str:
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else str(v)
    except (TypeError, ValueError):
        return str(v)


def run(df: pd.DataFrame, params: dict[str, Any], parquet: str = "data.parquet") -> Outcome:
    source, target = params.get("source"), params.get("target")
    weight, year = params.get("weight") or None, params.get("year") or None
    group, target_group = params.get("group") or None, params.get("target_group") or None
    directed = bool(params.get("directed"))
    if not source or not target or source == target:
        raise StatsError("网络指标需要两个不同的列：起点（source）与终点（target）")
    needed = [source, target, *[c for c in (weight, year) if c]]
    data, dropped = listwise(df, needed)
    extra = [c for c in (group, target_group) if c]
    if extra:
        from .stats import require
        require(df, extra)
        data = df.loc[data.index, list(dict.fromkeys(needed + extra))]
    if weight and not pd.api.types.is_numeric_dtype(data[weight]):
        raise StatsError(f"权重列 {weight} 不是数值型")
    if data.empty:
        raise StatsError("去掉缺失值后没有边")
    periods = sorted(data[year].unique().tolist()) if year else [None]
    if len(periods) > 80:
        raise StatsError(f"时间取值过多（{len(periods)}），请先按年汇总")
    groups = node_groups(data, source, target, group, target_group) if (group or target_group) else {}
    graph_rows, per_period, last_nodes, last_G, last_btw = [], {}, [], None, {}
    for per in periods:
        part = data if per is None else data[data[year] == per]
        G = build_graph(part, source, target, weight, directed)
        if G.number_of_nodes() > MAX_NODES:
            raise StatsError(f"节点过多（{G.number_of_nodes()}），超过 {MAX_NODES}")
        if G.number_of_nodes() == 0:
            continue
        g, nodes = measure(G)
        label = "全部" if per is None else _period_label(per)
        graph_rows.append([label, g["nodes"], g["edges"], g["density"], g["degree_centralization"],
                           g["betweenness_centralization"], g["components"], int(len(part))])
        per_period[label] = {r[0]: r[2] for r in nodes}
        last_nodes, last_G, last_btw = nodes, G, {r[0]: r[2] for r in nodes}
    if last_G is None:
        raise StatsError("没有可用的边（自环已忽略）")
    n_edges = int(len(data))
    last_label = graph_rows[-1][0]
    out = Outcome(summary={"n": n_edges, "missing_dropped": dropped, "periods": len(graph_rows),
                           "nodes": graph_rows[-1][1], "edges": graph_rows[-1][2], "directed": directed,
                           "degree_centralization": graph_rows[-1][4], "betweenness_centralization": graph_rows[-1][5]})
    out.tables.append(table("整体网络指标" + ("（按期）" if year else ""),
                            ["时期", "节点", "边", "密度", "度数中心势", "中介中心势", "连通分量", "边记录 n"], graph_rows,
                            n=n_edges, missing=dropped,
                            note="Freeman 中心势；中介中心度已标准化。" + ("有向网络按总度数计算度数中心势。" if directed else "")))
    top = last_nodes[:40]
    out.tables.append(table(f"节点中心度（{last_label}，按中介中心度排序，前 {len(top)} 个）", ["节点", "度数", "中介中心度", "接近中心度"],
                            top, n=len(last_nodes), missing=dropped))
    if year and len(per_period) > 1:
        mean_b: dict[str, float] = {}
        for vals in per_period.values():
            for k, v in vals.items():
                mean_b[k] = mean_b.get(k, 0.0) + v / len(per_period)
        focus = [k for k, _ in sorted(mean_b.items(), key=lambda kv: -kv[1])[:12]]
        labels = list(per_period)
        out.tables.append(table("中介中心度随时间变化（平均最高的节点）", ["时期", *focus],
                                [[lab, *[per_period[lab].get(k) for k in focus]] for lab in labels], n=n_edges, missing=dropped,
                                note="空白 = 该节点在此期不在网络中。"))
    if groups:
        br = brokerage(last_G, groups)
        rows = [[b, groups[b], *[br[b][r] for r in ROLES], sum(br[b].values())] for b in br]
        rows = [r for r in sorted(rows, key=lambda r: -r[-1]) if r[-1] > 0][:40]
        ungrouped = sum(1 for v in last_G.nodes if v not in groups)
        out.tables.append(table(f"Gould–Fernandez 中介角色（{last_label}）", ["节点", "组", *[ROLE_LABELS[r] for r in ROLES], "合计"], rows,
                                n=len(br), missing=ungrouped,
                                note=("无向网络：每条两步路径按两个方向各计一次，守门人与代表计数相同。" if not directed else "")
                                + (f"{ungrouped} 个节点没有组，未参与计数。" if ungrouped else "")
                                + ("" if rows else "没有符合条件的两步路径（a–b–c 且 a、c 不相连，三者都有组）。")))
        out.summary["brokerage"] = True
    # figure: drawing of the last period, node size by betweenness; centralization over time when per-year
    multi = len(graph_rows) > 1
    fig = new_figure(9.6 if multi else 6.4, 4.6)
    ax = fig.add_subplot(1, 2 if multi else 1, 1)
    pos = nx.spring_layout(last_G, seed=42)
    sizes = [60 + 1400 * last_btw.get(v, 0) for v in last_G.nodes]
    colors = None
    if groups:
        cats = sorted(set(groups.values()))
        palette = ["#2f6b5c", "#a86a14", "#3b6ea8", "#b03a2b", "#7a5ea8", "#5a9a86", "#8a8a8a"]
        colors = [palette[cats.index(groups[v]) % len(palette)] if v in groups else "#cccccc" for v in last_G.nodes]
    nx.draw_networkx_edges(last_G, pos, ax=ax, alpha=0.35, width=0.8, arrows=directed)
    nx.draw_networkx_nodes(last_G, pos, ax=ax, node_size=sizes, node_color=colors or "#5a9a86", alpha=0.9)
    if last_G.number_of_nodes() <= 60:
        nx.draw_networkx_labels(last_G, pos, ax=ax, font_size=7, font_family=_font_family())
    ax.set_title(f"{last_label}：{last_G.number_of_nodes()} 个节点，{last_G.number_of_edges()} 条边（大小 = 中介中心度）", fontsize=9)
    ax.axis("off")
    if multi:
        ax2 = fig.add_subplot(1, 2, 2)
        xs = [r[0] for r in graph_rows]
        ax2.plot(xs, [r[4] if r[4] is not None else float("nan") for r in graph_rows], marker="o", label="度数中心势")
        ax2.plot(xs, [r[5] if r[5] is not None else float("nan") for r in graph_rows], marker="s", label="中介中心势")
        ax2.set_ylim(0, 1)
        ax2.legend(fontsize=8)
        ax2.tick_params(axis="x", rotation=45, labelsize=7)
        ax2.set_title("Freeman 中心势随时间", fontsize=9)
    fig.tight_layout()
    out.figure = fig
    out.code = _code(parquet, source, target, weight, year, group, target_group, directed, needed, extra)
    return out


def _font_family() -> str:
    from matplotlib import rcParams
    fams = rcParams.get("font.sans-serif") or ["DejaVu Sans"]
    return fams[0]


def _code(parquet, source, target, weight, year, group, target_group, directed, needed, extra) -> str:
    funcs = "\n\n".join(inspect.getsource(f) for f in (node_id, build_graph, degree_centralization, betweenness_centralization,
                                                        brokerage, measure, node_groups))
    lines = [header(parquet, ["import networkx as nx"]), "", funcs, "",
             f"needed = {needed!r}",
             "data = df.dropna(subset=needed)            # edges with a missing endpoint/weight/period are dropped",
             f"data = df.loc[data.index, list(dict.fromkeys(needed + {extra!r}))]" if extra else "",
             "print('edges =', len(data), '| dropped for missing values:', len(df) - len(data))",
             f"groups = node_groups(data, {source!r}, {target!r}, {group!r}, {target_group!r})" if extra else "groups = {}",
             f"periods = sorted(data[{year!r}].unique()) if {year!r} else [None]" if year else "periods = [None]",
             "for per in periods:",
             f"    part = data if per is None else data[data[{year!r}] == per]" if year else "    part = data",
             f"    G = build_graph(part, {source!r}, {target!r}, {weight!r}, directed={directed!r})",
             "    graph, nodes = measure(G)",
             "    print(per, graph)",
             "    print(pd.DataFrame(nodes, columns=['node', 'degree', 'betweenness', 'closeness']).head(20))",
             "    if groups:",
             "        print(pd.DataFrame(brokerage(G, groups)).T)",
             "",
             "# drawing of the last period",
             "import matplotlib.pyplot as plt",
             "btw = nx.betweenness_centrality(G, normalized=True)",
             "nx.draw_networkx(G, nx.spring_layout(G, seed=42), node_size=[60 + 1400 * btw[v] for v in G.nodes], font_size=7)",
             "plt.axis('off'); plt.savefig('network.png', dpi=144)"]
    return "\n".join(l for l in lines if l is not None) + "\n"
