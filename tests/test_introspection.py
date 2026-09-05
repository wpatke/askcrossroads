"""Structure, meaning and date ranges are discovered from the file (spec §2, §6).

The rule these tests exist to protect is spec §2C: nothing about the schema is assumed.
Where a test names a table or a column it is naming what the FIXTURE built, which is test
input — src/ still contains none of these names.
"""

import pytest

from askcrossroads.database import Database
from askcrossroads.introspection import is_temporal

from conftest import AADF, BANK_HOLIDAYS, ERA5_WEATHER, STATS19


@pytest.fixture
def context(crossroads_db):
    """The context for a valid build carrying every dataset."""
    with Database.open(crossroads_db) as database:
        yield database.context


def names(relations):
    return {relation.name for relation in relations}


# --- The type rule, in isolation (spec §6) ----------------------------------------


@pytest.mark.parametrize(
    "data_type, expected",
    [
        ("DATE", True),
        ("TIMESTAMP", True),
        ("TIMESTAMP WITH TIME ZONE", True),
        ("TIMESTAMP_MS", True),
        ("timestamp", True),      # normalised, not case-sensitive
        ("VARCHAR", False),
        ("INTEGER", False),
        ("TIME", False),          # a time of day carries no calendar
        ("INTERVAL", False),      # a duration, not a moment
        ("", False),
        (None, False),
    ],
)
def test_temporal_types_are_recognised_by_type_alone(data_type, expected):
    assert is_temporal(data_type) is expected


# --- Structure discovery (spec §2A) -----------------------------------------------


def test_every_relation_is_discovered(context):
    discovered = names(context.relations)
    assert {"collisions", "vehicles", "codebook", "column_manifest"} <= discovered
    assert {"collisions_labelled", "collisions_spatial"} <= discovered


def test_tables_and_views_are_told_apart(context):
    assert "collisions" in names(context.tables)
    assert "collisions" not in names(context.views)
    assert "collisions_labelled" in names(context.views)
    assert "collisions_labelled" not in names(context.tables)


def test_columns_carry_the_type_the_database_declares(context):
    collisions = context.relation("collisions")
    types = {column.name: column.data_type for column in collisions.columns}
    assert types["datetime_local"] == "TIMESTAMP"
    assert types["collision_severity"] == "INTEGER"
    assert types["local_authority_ons_district"] == "VARCHAR"


def test_columns_are_in_declaration_order(context):
    collisions = context.relation("collisions")
    assert collisions.columns[0].name == "source_row_key"


@pytest.mark.parametrize(
    "datasets, present, absent",
    [
        ((STATS19,), set(), {"weather", "aadf", "bank_holidays"}),
        ((STATS19, ERA5_WEATHER), {"weather"}, {"aadf", "bank_holidays"}),
        ((STATS19, AADF), {"aadf"}, {"weather", "bank_holidays"}),
        ((STATS19, BANK_HOLIDAYS), {"bank_holidays"}, {"weather", "aadf"}),
    ],
)
def test_absent_datasets_are_simply_absent(crossroads_db_factory, datasets, present, absent):
    """Spec §11.4: a weatherless build must be handled without error, and must not
    offer a table it does not have."""
    with Database.open(crossroads_db_factory(datasets=datasets)) as database:
        discovered = names(database.context.relations)
        assert present <= discovered
        assert not (absent & discovered)
        for missing in absent:
            assert database.context.relation(missing) is None


def test_nothing_is_filtered_out_of_discovery(context):
    """Everything information_schema reports is carried, audit tables included.

    Filtering by name would be hardcoded knowledge of Crossroads-UK's naming (spec §2C)
    and would break silently the day upstream renamed something.
    """
    assert "quarantine_raw" in names(context.relations)


def test_a_column_absent_from_the_manifest_is_still_discovered(context):
    """The manifest classifies STATS19 columns only, so this is the common case.

    Structure comes from information_schema, not from the manifest, which is why an
    unfamiliar or absent classification can never lose a column.
    """
    weather = context.relation("weather")
    assert "temperature_c" in {column.name for column in weather.columns}
    assert "temperature_c" not in {entry.col for entry in context.column_manifest}


# --- Date ranges, keyed on type and never on name (spec §6, §2C) ------------------


def ranges_by_key(context):
    return {(r.relation, r.column): r for r in context.temporal_ranges}


def test_a_timestamp_column_gets_a_range(context):
    found = ranges_by_key(context)[("collisions", "datetime_local")]
    assert found.earliest is not None
    assert found.latest is not None
    assert found.earliest <= found.latest


def test_a_date_column_gets_a_range(context):
    """The first trap: a DATE column, which code looking only for TIMESTAMP would miss."""
    found = ranges_by_key(context)[("bank_holidays", "date")]
    assert found.earliest is not None
    assert found.latest is not None


def test_a_text_column_with_a_date_like_name_gets_no_range(context):
    """The second trap: `date_raw` is VARCHAR. Matching on names would wrongly take it."""
    assert ("bank_holidays", "date_raw") not in ranges_by_key(context)


def test_no_range_is_computed_for_a_view(context):
    """Views are saved filters over tables; ranging them repeats work for no gain."""
    view_names = names(context.views)
    for temporal_range in context.temporal_ranges:
        assert temporal_range.relation not in view_names


def test_an_empty_table_yields_an_empty_range(context):
    """MIN and MAX of nothing are NULL, and that is a fact worth carrying: the column
    exists and holds nothing."""
    found = ranges_by_key(context)[("quarantine_raw", "ingested_at")]
    assert found.earliest is None
    assert found.latest is None


def test_ranges_reflect_the_data_actually_present(crossroads_db_factory):
    """The point of the whole mechanism: 2019 is recognisable as outside a 2023 build."""
    with Database.open(crossroads_db_factory(years=(2019,))) as database:
        found = ranges_by_key(database.context)[("collisions", "datetime_local")]
        assert found.earliest.year == 2019
        assert found.latest.year == 2019


def test_ranges_widen_with_the_years_in_the_build(crossroads_db_factory):
    with Database.open(crossroads_db_factory(years=(2019, 2023))) as database:
        found = ranges_by_key(database.context)[("collisions", "datetime_local")]
        assert found.earliest.year == 2019
        assert found.latest.year == 2023


# --- Codebook and manifest (spec §2B, §3) -----------------------------------------


def test_the_whole_codebook_is_loaded(context):
    assert len(context.codebook) > 0
    variables = {entry.variable for entry in context.codebook}
    assert "collision_severity" in variables


def test_codes_are_strings_and_missing_sentinels_are_flagged(context):
    """Spec §3: the model must know which codes were nulled during the build."""
    assert all(isinstance(entry.code, str) for entry in context.codebook)
    assert any(entry.is_missing for entry in context.codebook)
    assert all(isinstance(entry.is_missing, bool) for entry in context.codebook)


def test_the_codebook_is_sorted_explicitly(context):
    keys = [(entry.variable, entry.code) for entry in context.codebook]
    assert keys == sorted(keys)


def test_the_manifest_is_loaded_and_sorted(context):
    keys = [(entry.tbl, entry.col) for entry in context.column_manifest]
    assert keys == sorted(keys)
    assert len(context.column_manifest) > 0


def test_coded_columns_are_derived_from_the_manifest(context):
    assert "collision_severity" in context.coded_column_names
    # The manifest classifies this numeric, so it is not a coded column even though the
    # codebook carries it. A speed limit stays a number (spec §3).
    assert "speed_limit" not in context.coded_column_names


def test_manifest_tbl_is_the_source_name_not_a_relation_name(context):
    """A join from manifest.tbl to a relation name would match nothing."""
    sources = {entry.tbl for entry in context.column_manifest}
    assert not (sources & names(context.relations))


def test_every_kind_the_build_uses_is_carried_through(context, manifest_kinds):
    """Nothing is dropped for having an unfamiliar classification."""
    assert set(context.manifest_kinds) <= set(manifest_kinds)
    assert "coded" in context.manifest_kinds


# --- The context is immutable and deterministic (spec §3) -------------------------


def test_every_field_is_a_tuple(context):
    """A list inside a frozen dataclass is still mutable, which would defeat the point."""
    assert isinstance(context.relations, tuple)
    assert isinstance(context.codebook, tuple)
    assert isinstance(context.column_manifest, tuple)
    assert isinstance(context.temporal_ranges, tuple)
    assert isinstance(context.relation("collisions").columns, tuple)


def test_the_context_cannot_be_reassigned(context):
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        context.relations = ()


def test_two_opens_of_one_file_produce_identical_contexts(crossroads_db):
    """The foundation of spec §3's byte-stable prompt prefix.

    If this ever fails, the cache breaks silently and nothing else would catch it.
    """
    with Database.open(crossroads_db) as first, Database.open(crossroads_db) as second:
        assert first.context.relations == second.context.relations
        assert first.context.codebook == second.context.codebook
        assert first.context.column_manifest == second.context.column_manifest
        assert first.context.temporal_ranges == second.context.temporal_ranges
        assert first.context.provenance == second.context.provenance
