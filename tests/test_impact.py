"""Blast radius: the context existing tools are blind to."""

from conftest import VULNERABLE

from caliper.analysis.impact import build_graph
from caliper.analysis.structure import make_source_file


def graph_of(sources):
    return build_graph([make_source_file(p, t) for p, t in sorted(sources.items())])


def test_hub_outranks_an_unreferenced_script():
    graph = graph_of(VULNERABLE)
    assert graph.blast_radius("svc/auth.py") > graph.blast_radius("scripts/oneoff.py")
    assert graph.blast_radius("scripts/oneoff.py") == 0.0


def test_transitive_dependents_are_counted():
    graph = graph_of(VULNERABLE)
    # api -> db -> auth, so auth is reached by both.
    assert graph.transitive_dependents("svc/auth.py") == {"svc/db.py", "svc/api.py"}


def test_blast_radius_is_bounded():
    graph = graph_of(VULNERABLE)
    for path in graph.files:
        assert 0.0 <= graph.blast_radius(path) <= 1.0


def test_single_file_submission_has_no_blast_radius():
    graph = graph_of({"only.py": "x = 1\n"})
    assert graph.blast_radius("only.py") == 0.0


def test_imports_resolve_when_the_tree_is_not_rooted_at_the_package():
    """Files collected as examples/demo/svc/auth.py are still imported as svc.auth."""
    sources = {f"examples/demo/{path}": text for path, text in VULNERABLE.items()}
    graph = graph_of(sources)
    assert graph.dependents("examples/demo/svc/auth.py") == 2


def test_ambiguous_names_do_not_create_edges():
    """Two files answering to `utils` must not be guessed between."""
    sources = {
        "a/utils.py": "x = 1\n",
        "b/utils.py": "y = 2\n",
        "main.py": "import utils\n",
    }
    graph = graph_of(sources)
    assert graph.edges["main.py"] == set()


def test_cycles_terminate():
    sources = {"a.py": "import b\n", "b.py": "import a\n"}
    graph = graph_of(sources)
    assert graph.dependents("a.py") == 1
    assert graph.dependents("b.py") == 1


def test_external_imports_are_not_edges():
    graph = graph_of({"a.py": "import os\nimport requests\n"})
    assert graph.edges["a.py"] == set()
    assert set(graph.unresolved["a.py"]) == {"os", "requests"}


def test_polyglot_graph():
    sources = {
        "server/main.go": 'package main\nimport "server/handler"\n',
        "server/handler.go": "package handler\nfunc H() {}\n",
        "web/app.js": "import { x } from './util'\n",
        "web/util.js": "export const x = 1\n",
    }
    graph = graph_of(sources)
    assert graph.dependents("server/handler.go") == 1
    assert graph.dependents("web/util.js") == 1
