from sqlalchemy import func, select

from app.commands.prune_non_target_regions import apply_region_prune, audit_company_regions
from app.models import Company
from app.services.checko import CompanyPayload, OkvedItem
from app.services.discovery import upsert_company


class FakeCheckoClient:
    def __init__(self, regions: dict[str, tuple[str | None, str | None]]):
        self.regions = regions

    def get_company(self, inn: str) -> CompanyPayload:
        region_code, region_name = self.regions[inn]
        return CompanyPayload(
            name=f"Компания {inn}",
            inn=inn,
            ogrn=None,
            primary_okved=OkvedItem("49.41"),
            region_code=region_code,
            region_name=region_name,
        )


def add_company(db, inn: str) -> None:
    upsert_company(
        db,
        CompanyPayload(
            name=f"Компания {inn}",
            inn=inn,
            ogrn=None,
            primary_okved=OkvedItem("49.41"),
        ),
    )


def test_region_prune_deletes_only_confirmed_non_target_companies(db):
    add_company(db, "7700000001")
    add_company(db, "5000000002")
    add_company(db, "0100000003")
    db.commit()

    audit = audit_company_regions(
        db,
        FakeCheckoClient(
            {
                "7700000001": ("77", "Москва"),
                "5000000002": ("50", "Московская область"),
                "0100000003": ("01", "Адыгея"),
            }
        ),
    )

    assert [item.company.inn for item in audit.kept] == ["7700000001", "5000000002"]
    assert [item.company.inn for item in audit.removed] == ["0100000003"]
    assert apply_region_prune(db, audit) == 1
    assert db.scalar(select(func.count(Company.id))) == 2


def test_region_prune_refuses_delete_when_region_is_unknown(db):
    add_company(db, "0100000003")
    db.commit()

    audit = audit_company_regions(
        db,
        FakeCheckoClient({"0100000003": (None, None)}),
    )

    assert audit.errors == ["ИНН 0100000003: Checko не вернул код региона"]
    try:
        apply_region_prune(db, audit)
    except RuntimeError as exc:
        assert "Удаление отменено" in str(exc)
    else:
        raise AssertionError("apply_region_prune must reject an incomplete audit")
    assert db.scalar(select(func.count(Company.id))) == 1
