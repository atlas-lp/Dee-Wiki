"""
tests/test_graph_scripts.py — Tests for scripts/graph.py pure functions.
"""

from unittest.mock import patch

import pytest

import graph as graph_module
from graph import (
    parse_frontmatter,
    strip_frontmatter,
    get_neighbors,
    build_graph,
)


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------

def test_parse_frontmatter_valid():
    content = "---\ntype: concept\naliases: [Foo, F]\ntags: [finance]\n---\n\n# Title\n"
    fm = parse_frontmatter(content)
    assert fm.get("type") == "concept"


def test_parse_frontmatter_empty_string():
    assert parse_frontmatter("") == {}


def test_parse_frontmatter_no_fences():
    assert parse_frontmatter("type: concept\ntags: [a]") == {}


def test_parse_frontmatter_unclosed():
    content = "---\ntype: concept\n# no closing fence"
    assert parse_frontmatter(content) == {}


def test_parse_frontmatter_inline_list():
    content = "---\ntags: [a, b, c]\n---\n"
    fm = parse_frontmatter(content)
    tags = fm.get("tags", [])
    assert isinstance(tags, list)
    assert len(tags) == 3


def test_parse_frontmatter_multiline_relationships():
    content = "---\nrelationships:\n  - target: foo\n    type: uses\n---\n"
    fm = parse_frontmatter(content)
    rels = fm.get("relationships", [])
    assert isinstance(rels, list)
    assert len(rels) == 1
    rel = rels[0]
    assert isinstance(rel, dict)
    assert rel.get("target") == "foo"
    assert rel.get("type") == "uses"


# ---------------------------------------------------------------------------
# strip_frontmatter
# ---------------------------------------------------------------------------

def test_strip_frontmatter_removes_block():
    content = "---\ntype: concept\n---\n\n# Hello\n\nBody text.\n"
    result = strip_frontmatter(content)
    assert "type:" not in result
    assert "Hello" in result


def test_strip_frontmatter_no_frontmatter_unchanged():
    content = "# Just a heading\n\nSome text."
    result = strip_frontmatter(content)
    assert result == content


# ---------------------------------------------------------------------------
# get_neighbors
# ---------------------------------------------------------------------------

def test_get_neighbors_outgoing_only(minimal_graph):
    nbrs = get_neighbors(minimal_graph, "a", direction="outgoing")
    targets = {n["node"] for n in nbrs}
    assert "b" in targets
    assert "c" in targets
    for n in nbrs:
        assert n["direction"] == "outgoing"


def test_get_neighbors_incoming_only(minimal_graph):
    # "c" is the target of a→c (depends_on) and b→c (related_to)
    nbrs = get_neighbors(minimal_graph, "c", direction="incoming")
    sources = {n["node"] for n in nbrs}
    assert "a" in sources
    assert "b" in sources


def test_get_neighbors_both(minimal_graph):
    # "b" has incoming from "a" and outgoing to "c"
    nbrs = get_neighbors(minimal_graph, "b", direction="both")
    nodes = {n["node"] for n in nbrs}
    assert "a" in nodes
    assert "c" in nodes


def test_get_neighbors_edge_type_filter(minimal_graph):
    nbrs = get_neighbors(minimal_graph, "a", edge_types=["uses"], direction="outgoing")
    assert all(n["edge_type"] == "uses" for n in nbrs)
    targets = {n["node"] for n in nbrs}
    assert "b" in targets
    assert "c" not in targets  # a→c is depends_on, not uses


def test_get_neighbors_nonexistent_node(minimal_graph):
    nbrs = get_neighbors(minimal_graph, "z", direction="both")
    assert nbrs == []


# ---------------------------------------------------------------------------
# build_graph (integration against tmp_wiki_dir)
# ---------------------------------------------------------------------------

def test_build_graph_from_tmp_wiki(tmp_wiki_dir):
    with patch.object(graph_module, "WIKI_DIR", tmp_wiki_dir), \
         patch.object(graph_module, "VAULT",    tmp_wiki_dir.parent):
        g = build_graph()
    assert len(g["nodes"]) == 2
    slugs = set(g["nodes"].keys())
    assert "alpha" in slugs
    assert "beta"  in slugs


def test_build_graph_edges_from_frontmatter(tmp_wiki_dir):
    with patch.object(graph_module, "WIKI_DIR", tmp_wiki_dir), \
         patch.object(graph_module, "VAULT",    tmp_wiki_dir.parent):
        g = build_graph()
    # alpha.md has relationship target: beta, type: uses
    edges = [(e["from"], e["to"], e["type"]) for e in g["edges"]]
    assert ("alpha", "beta", "uses") in edges


def test_build_graph_empty_dir(tmp_path):
    empty_wiki = tmp_path / "wiki"
    empty_wiki.mkdir()
    with patch.object(graph_module, "WIKI_DIR", empty_wiki):
        g = build_graph()
    assert g["nodes"] == {}
    assert g["edges"] == []
