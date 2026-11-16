"""Schema registry, XSD validation and ``scripts/fetch_schemas.py`` tests.

Acceptance criteria covered here: validation against a minimal own XSD fixture, a missing
schema reported as SKIPPED (never an error), checksum verification of a download, and an
abort when the downloaded bytes do not match ``config/schemas.yaml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import fetch_schemas

from app.integrations import schemas
from app.integrations.schemas import SchemaConfigError, SchemaError, ValidationStatus

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINIMAL_XSD = FIXTURES / "schemas" / "minimal.xsd"
GOLDEN_FILE = FIXTURES / "fa3_expected.xml"

#: Bytes used as a stand-in for a downloaded XSD.
DOWNLOADED = b'<?xml version="1.0"?><xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'


@pytest.fixture(autouse=True)
def _clear_schema_cache() -> None:
    schemas.clear_cache()
    yield
    schemas.clear_cache()


# ---------------------------------------------------------------- registry


def test_registry_describes_the_fa3_format() -> None:
    spec = schemas.get("ksef_fa3")
    assert spec.version == "FA(3)"
    assert spec.namespace == "http://crd.gov.pl/wzor/2025/06/25/13775/"
    assert spec.kod_formularza == "FA"
    assert spec.kod_systemowy == "FA (3)"
    assert spec.wariant_formularza == 3
    assert spec.wersja_schemy == "1-0E"
    assert spec.url and spec.url.endswith("/13775/schemat.xsd")
    assert spec.sha256 is not None and len(spec.sha256) == 64
    assert spec.path.name == "FA3.xsd"
    assert spec.path.is_absolute()
    assert schemas.fa3_format().version == spec.version


def test_fa3_xsd_imports_are_pinned_by_checksum() -> None:
    spec = schemas.get("ksef_fa3")
    assert len(spec.imports) == 3
    filenames = {item.filename for item in spec.imports}
    assert filenames == {
        "StrukturyDanych_v10-0E.xsd",
        "ElementarneTypyDanych_v10-0E.xsd",
        "KodyKrajow_v10-0E.xsd",
    }
    assert all(len(item.sha256) == 64 for item in spec.imports)


def test_jpk_pit_is_optional_and_not_configured() -> None:
    spec = schemas.get("jpk_pit")
    assert spec.optional is True
    assert spec.url is None and spec.sha256 is None
    assert spec.path.name == "JPK_PIT.xsd"


def test_a_url_requires_a_checksum(tmp_path: Path) -> None:
    config = tmp_path / "schemas.yaml"
    config.write_text(
        'broken:\n  url: "https://example.invalid/x.xsd"\n  path: "resources/x.xsd"\n',
        encoding="utf-8",
    )
    with pytest.raises(SchemaConfigError, match="sha256"):
        schemas.load_config(config_path=config)


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "schemas.yaml"
    config.write_text('broken:\n  path: "x.xsd"\n  surprise: 1\n', encoding="utf-8")
    with pytest.raises(SchemaConfigError, match="unknown fields"):
        schemas.load_config(config_path=config)


# ---------------------------------------------------------------- XSD validation


def test_validate_accepts_a_conforming_document() -> None:
    xml = GOLDEN_FILE.read_bytes()
    assert schemas.validate(xml, MINIMAL_XSD) == []
    result = schemas.check(xml, MINIMAL_XSD)
    assert result.status is ValidationStatus.OK
    assert result.passed is True
    assert result.as_dict() == {"status": "ok", "violations": []}


def test_validate_reports_every_violation() -> None:
    result = schemas.check(b"<invoice/>", MINIMAL_XSD)
    assert result.status is ValidationStatus.FAILED
    assert result.passed is False
    assert result.violations
    first = result.violations[0]
    assert first.path.startswith("line ")
    assert "invoice" in first.message
    assert set(first.as_dict()) == {"path", "message"}


def test_malformed_xml_is_a_violation_not_an_exception() -> None:
    result = schemas.check(b"<Faktura>", MINIMAL_XSD)
    assert result.status is ValidationStatus.FAILED
    assert [violation.path for violation in result.violations] == ["document"]


def test_missing_schema_is_skipped(tmp_path: Path) -> None:
    missing = tmp_path / "missing.xsd"
    assert schemas.validate(b"<invoice/>", missing) == []
    result = schemas.check(b"<invoice/>", missing)
    assert result.status is ValidationStatus.SKIPPED
    assert result.violations == ()
    assert result.passed is True
    assert schemas.check(b"<invoice/>", None).status is ValidationStatus.SKIPPED


def test_unusable_schema_raises(tmp_path: Path) -> None:
    broken = tmp_path / "broken.xsd"
    broken.write_text("<xs:schema", encoding="utf-8")
    with pytest.raises(SchemaError):
        schemas.check(b"<invoice/>", broken)


def test_unresolvable_import_raises(tmp_path: Path) -> None:
    """A schema whose imported XSD was never downloaded is an operator error, not a 422."""
    root = tmp_path / "root.xsd"
    root.write_text(
        '<?xml version="1.0"?>'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'targetNamespace="urn:test" xmlns:t="urn:test">'
        '<xs:import namespace="urn:other" schemaLocation="http://example.invalid/other.xsd"/>'
        '<xs:element name="doc" type="xs:string"/>'
        "</xs:schema>",
        encoding="utf-8",
    )
    with pytest.raises(SchemaError, match="could not be compiled"):
        schemas.check(b'<doc xmlns="urn:test">x</doc>', root)


def test_golden_file_is_valid_against_the_downloaded_fa3_schema() -> None:
    """End-to-end check with the real MF schema (skipped until it is downloaded)."""
    spec = schemas.get("ksef_fa3")
    if not spec.available:
        pytest.skip("resources/ksef/FA3.xsd is not downloaded; run scripts/fetch_schemas.py")
    result = schemas.check(GOLDEN_FILE.read_bytes(), spec.path)
    assert result.status is ValidationStatus.OK, [item.as_dict() for item in result.violations]


# ---------------------------------------------------------------- fetch_schemas.py


def test_verify_checksum() -> None:
    digest = fetch_schemas.sha256_digest(DOWNLOADED)
    fetch_schemas.verify_checksum(DOWNLOADED, digest)
    with pytest.raises(fetch_schemas.ChecksumMismatch):
        fetch_schemas.verify_checksum(DOWNLOADED, "0" * 64)


def test_fetch_writes_a_verified_file(tmp_path: Path) -> None:
    target = tmp_path / "FA3.xsd"
    written = fetch_schemas.fetch(
        "https://example.invalid/FA3.xsd",
        fetch_schemas.sha256_digest(DOWNLOADED),
        target,
        downloader=lambda url: DOWNLOADED,
    )
    assert written is True
    assert target.read_bytes() == DOWNLOADED
    assert not list(tmp_path.glob("*.part"))


def test_fetch_aborts_on_a_checksum_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "FA3.xsd"
    with pytest.raises(fetch_schemas.ChecksumMismatch, match="aborting"):
        fetch_schemas.fetch(
            "https://example.invalid/FA3.xsd",
            "0" * 64,
            target,
            downloader=lambda url: DOWNLOADED,
        )
    assert not target.exists()
    assert not list(tmp_path.iterdir())


def test_fetch_skips_an_up_to_date_file(tmp_path: Path) -> None:
    target = tmp_path / "FA3.xsd"
    target.write_bytes(DOWNLOADED)

    def _never(url: str) -> bytes:  # pragma: no cover - must not be called
        raise AssertionError("an up-to-date file must not be downloaded again")

    assert (
        fetch_schemas.fetch(
            "https://example.invalid/FA3.xsd",
            fetch_schemas.sha256_digest(DOWNLOADED),
            target,
            downloader=_never,
        )
        is False
    )


def _registry(tmp_path: Path, *, digest: str, optional: bool = False) -> tuple[Path, Path]:
    target = tmp_path / "FA3.xsd"
    url = "null" if optional else '"https://example.invalid/FA3.xsd"'
    config = tmp_path / "schemas.yaml"
    config.write_text(
        "\n".join(
            [
                "ksef_fa3:",
                '  version: "FA(3)"',
                '  namespace: "urn:test:fa3"',
                '  kod_formularza: "FA"',
                '  kod_systemowy: "FA (3)"',
                "  wariant_formularza: 3",
                '  wersja_schemy: "1-0E"',
                f"  url: {url}",
                f'  sha256: "{digest}"',
                f'  path: "{target.as_posix()}"',
                "  optional: true" if optional else "",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config, target


def test_cli_check_mode_verifies_local_copies(tmp_path: Path) -> None:
    digest = fetch_schemas.sha256_digest(DOWNLOADED)
    config, target = _registry(tmp_path, digest=digest)
    assert fetch_schemas.main(["--config", str(config), "--check"]) == 1

    target.write_bytes(DOWNLOADED)
    assert fetch_schemas.main(["--config", str(config), "--check"]) == 0

    target.write_bytes(b"tampered")
    assert fetch_schemas.main(["--config", str(config), "--check"]) == 1


def test_cli_skips_an_optional_target_without_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, target = _registry(tmp_path, digest="0" * 64, optional=True)
    assert fetch_schemas.main(["--config", str(config)]) == 0
    assert "skipped" in capsys.readouterr().out
    assert not target.exists()


def test_cli_downloads_and_aborts_on_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = fetch_schemas.sha256_digest(DOWNLOADED)
    config, target = _registry(tmp_path, digest=digest)
    monkeypatch.setattr(fetch_schemas, "download", lambda url: DOWNLOADED)
    assert fetch_schemas.main(["--config", str(config)]) == 0
    assert target.read_bytes() == DOWNLOADED

    target.unlink()
    monkeypatch.setattr(fetch_schemas, "download", lambda url: b"something else")
    assert fetch_schemas.main(["--config", str(config)]) == 2
    assert not target.exists()


def test_cli_rejects_an_unknown_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _ = _registry(tmp_path, digest=fetch_schemas.sha256_digest(DOWNLOADED))
    assert fetch_schemas.main(["--config", str(config), "--only", "nope"]) == 2
    assert "unknown schema" in capsys.readouterr().err


def test_repository_registry_is_downloadable_configuration() -> None:
    """Every non-optional entry in the shipped registry has a URL and a checksum."""
    for key, spec in schemas.load_config().items():
        if spec.optional:
            continue
        assert spec.url and spec.sha256, key
        assert spec.path.is_absolute(), key
        assert len(spec.sha256) == 64, key

