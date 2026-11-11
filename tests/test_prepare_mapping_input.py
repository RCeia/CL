from klkg.config import read_json


def movies_by_id(settings):
    return {movie["tmdb_id"]: movie for movie in read_json(settings.path("interim") / "movies.json")}


def test_selection_follows_manifest_order_and_skips_missing_overview(prepared):
    selection = read_json(prepared.path("interim") / "selection.json")
    assert selection["movie_ids"] == [900001, 900002]
    assert selection["movies_with_wikidata"] == 2


def test_final_size_takes_a_prefix_of_the_snapshot(project, tmp_path):
    from klkg.collection.prepare_mapping_input import prepare

    dataset = {**project["dataset"], "final_size": 1}
    summary = prepare(project.path("raw_tmdb"), project.path("raw_wikidata"), tmp_path / "interim", dataset, True)
    assert summary["movie_ids"] == [900001]


def test_nested_records_carry_the_movie_id(prepared):
    for movie_id, movie in movies_by_id(prepared).items():
        for key in ("genres", "keywords", "collections", "production_companies", "production_countries", "spoken_languages", "cast", "directors", "writers"):
            assert all(record["movie_id"] == movie_id for record in movie[key]), key


def test_cast_depth_and_crew_roles(prepared):
    movie = movies_by_id(prepared)[900001]
    assert [member["order"] for member in movie["cast"]] == list(range(10))
    assert [(member["person_id"], member["job"]) for member in movie["directors"]] == [(200, "Director")]
    assert sorted((member["person_id"], member["job"]) for member in movie["writers"]) == [(200, "Screenplay"), (201, "Novel")]
    assert movie["cast"][3]["character"] is None


def test_empty_values_become_null(prepared):
    movie = movies_by_id(prepared)[900002]
    for key in ("release_date", "runtime", "budget", "revenue", "vote_average", "original_language", "imdb_id"):
        assert movie[key] is None, key
    assert movie["wikidata_id"] == "Q999000002"
    assert [language["iso_639_1"] for language in movie["spoken_languages"]] == ["es"]


def test_people_countries_languages_and_keywords(prepared):
    interim = prepared.path("interim")
    persons = {person["tmdb_id"]: person for person in read_json(interim / "persons.json")}
    assert set(persons) == {*range(100, 110), 200, 201, 300, 301}
    assert persons[100]["wikidata_id"] == "Q999100200".replace("200", "100")
    assert persons[201]["gender"] is None
    assert persons[300]["gender"] == "non-binary"
    assert [(country["iso"], country["wikidata_id"]) for country in read_json(interim / "countries.json")] == [("ES", "Q29"), ("US", "Q30")]
    assert [language["iso"] for language in read_json(interim / "languages.json")] == ["en", "es"]
    assert read_json(interim / "keyword_frequencies.json")[0] == {"id": 9001, "name": "detective", "films": 2}
