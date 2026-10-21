from research_lab.files import artifacts as A


def test_migrate_and_today(conn, client):
    r = client.get("/")
    assert r.status_code == 200
    assert "年 第" in r.text and "周" in r.text


def test_mutation_guard(client):
    client.headers.pop("HX-Request")
    assert client.post("/nothing").status_code == 403
    assert client.post("/nothing", headers={"Origin": "http://127.0.0.1:4100"}).status_code == 404


def test_artifact_roundtrip_and_counts(repo, conn):
    from research_lab.config import get_paths
    p = get_paths()
    path = A.create("puzzle", "test-puzzle", "asean-centrality")
    a = A.load(path)
    assert a.ref.startswith("ART-") and not A.counts(a.body)
    A.append_to_section(a, "Evidence", "- x <!-- rl:EXC-2026-0001 -->")
    A.add_related(a, "ART-2026-4001")
    A.save(a)
    b = A.load(path)
    assert "rl:begin excerpts" in b.body and "ART-2026-4001" in str(b.front["related"])
    n = A.index(conn)
    assert n >= 2
    b.body = b.body.replace("## What did I figure out that I didn't know last week?\n\n<!-- One sentence. If you cannot write this line, the artifact is not done. -->\n\n...",
                            "## What did I figure out that I didn't know last week?\n\nSomething real.")
    b.body = b.body.replace("## Claim\n\n**I currently think:**\n\n> ...", "## Claim\n\n**I currently think:**\n\n> Centrality is a rule.")
    assert A.counts(b.body)
