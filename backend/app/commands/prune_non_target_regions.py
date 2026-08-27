import argparse
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import TARGET_REGION_CODES, Settings, get_settings
from app.database import SessionLocal
from app.models import Company
from app.services.checko import CheckoAPIError, CheckoClient


@dataclass(slots=True)
class RegionAuditItem:
    company: Company
    region_code: str
    region_name: str


@dataclass(slots=True)
class RegionAuditResult:
    kept: list[RegionAuditItem] = field(default_factory=list)
    removed: list[RegionAuditItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def audit_company_regions(db: Session, client: CheckoClient) -> RegionAuditResult:
    result = RegionAuditResult()
    companies = list(db.scalars(select(Company).order_by(Company.id)).all())

    for company in companies:
        try:
            payload = client.get_company(company.inn)
        except (CheckoAPIError, ValueError) as exc:
            result.errors.append(f"ИНН {company.inn}: {exc}")
            continue

        if not payload.region_code:
            result.errors.append(f"ИНН {company.inn}: Checko не вернул код региона")
            continue

        item = RegionAuditItem(
            company=company,
            region_code=payload.region_code,
            region_name=payload.region_name or "Без названия региона",
        )
        if payload.region_code in TARGET_REGION_CODES:
            result.kept.append(item)
        else:
            result.removed.append(item)

    return result


def apply_region_prune(db: Session, audit: RegionAuditResult) -> int:
    if audit.errors:
        raise RuntimeError("Удаление отменено: регион определён не для всех компаний")
    for item in audit.removed:
        db.delete(item.company)
    db.commit()
    return len(audit.removed)


def run_region_prune(settings: Settings, *, apply: bool) -> RegionAuditResult:
    with SessionLocal() as db:
        with CheckoClient(
            settings.checko_api_keys,
            settings.checko_base_url,
            settings.checko_timeout_seconds,
        ) as client:
            audit = audit_company_regions(db, client)

        print(f"Проверено компаний: {len(audit.kept) + len(audit.removed) + len(audit.errors)}")
        for item in audit.kept:
            print(
                f"ОСТАВИТЬ: id={item.company.id}, ИНН={item.company.inn}, "
                f"регион={item.region_code} {item.region_name}, {item.company.name}"
            )
        for item in audit.removed:
            print(
                f"УДАЛИТЬ: id={item.company.id}, ИНН={item.company.inn}, "
                f"регион={item.region_code} {item.region_name}, {item.company.name}"
            )
        for error in audit.errors:
            print(f"ОШИБКА: {error}")

        if apply:
            removed_count = apply_region_prune(db, audit)
            print(f"Удалено компаний: {removed_count}")
        else:
            print("Проверочный режим: база не изменена")
        return audit


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Проверить регионы через Checko и удалить компании вне Москвы и Московской области.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Применить удаление. Без флага выполняется только проверка.",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.checko_configured:
        parser.exit(1, "Ошибка: Checko API не настроен\n")

    try:
        audit = run_region_prune(settings, apply=args.apply)
    except (CheckoAPIError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Ошибка очистки регионов: {exc}\n")
    return 1 if audit.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
