import shutil
from pathlib import Path

import pytest

from klkg.collection.prepare_mapping_input import prepare
from klkg.config import Settings, load_settings

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def make_project(root: Path) -> Settings:
    for name in ("config", "mappings", "ontology"):
        shutil.copytree(REPO / name, root / name)
    shutil.copytree(FIXTURES / "raw_tmdb", root / "data" / "raw" / "tmdb")
    shutil.copytree(FIXTURES / "raw_wikidata", root / "data" / "raw" / "wikidata")
    return load_settings(root / "config" / "settings.yaml")


@pytest.fixture
def project(tmp_path: Path) -> Settings:
    return make_project(tmp_path)


@pytest.fixture(scope="module")
def prepared(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    project = make_project(tmp_path_factory.mktemp("project"))
    prepare(
        project.path("raw_tmdb"),
        project.path("raw_wikidata"),
        project.path("interim"),
        project["dataset"],
        project["collection"]["require_overview"],
    )
    return project
