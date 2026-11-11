from klkg.collection.wikidata_alignment import align_films, cached, pick_unique, resolve_keys


class FakeWikidata:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def select(self, query):
        self.queries.append(query)
        return self.rows


def test_film_alignment_prefers_wikidata_and_records_conflicts():
    tmdb = {"1": "Q10", "2": "Q20", "3": "Q30", "4": None, "5": "not-a-qid", "6": "Q60"}
    matches = {"1": ["Q10"], "2": ["Q21"], "4": ["Q40"], "6": ["Q5", "Q60"]}
    aligned, conflicts = align_films(tmdb, matches)
    assert aligned["1"] == {"qid": "Q10", "source": "wikidata_p4947"}
    assert aligned["2"] == {"qid": "Q21", "source": "wikidata_p4947"}
    assert aligned["3"] == {"qid": "Q30", "source": "tmdb_external_ids"}
    assert aligned["4"]["qid"] == "Q40"
    assert "5" not in aligned
    assert aligned["6"] == {"qid": "Q60", "source": "wikidata_p4947_ambiguous"}
    assert conflicts == [{"tmdb_id": "2", "tmdb_external_ids": "Q20", "wikidata_p4947": ["Q21"]}]


def test_batches_and_deterministic_choice():
    client = FakeWikidata([
        {"key": "7", "item": "http://www.wikidata.org/entity/Q900"},
        {"key": "7", "item": "http://www.wikidata.org/entity/Q12"},
    ])
    matches = resolve_keys(client, ["7", "8", "9"], "VALUES ?key { %s }", batch_size=2)
    assert len(client.queries) == 2
    assert '"7" "8"' in client.queries[0]
    chosen, ambiguous = pick_unique(matches)
    assert chosen == {"7": "Q12"}
    assert ambiguous == {"7": ["Q12", "Q900"]}


def test_cache_is_tied_to_the_snapshot(tmp_path):
    path = tmp_path / "persons.json"
    calls = []

    def compute():
        calls.append(1)
        return {"aligned": {"1": "Q1"}}

    assert cached(path, False, "s1", compute)["aligned"] == {"1": "Q1"}
    cached(path, False, "s1", compute)
    assert len(calls) == 1
    cached(path, False, "s2", compute)
    assert len(calls) == 2
    cached(path, True, "s2", compute)
    assert len(calls) == 3
