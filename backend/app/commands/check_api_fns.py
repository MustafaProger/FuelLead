import argparse
import json
from typing import Any

from app.config import get_settings
from app.services.api_fns import ApiFnsAPIError, ApiFnsClient


def _usage(payload: dict[str, Any], method: str) -> dict[str, int | str | None]:
    methods = payload.get("Методы") or {}
    row = methods.get(method) if isinstance(methods, dict) else {}
    row = row or {}
    if not isinstance(row, dict):
        row = {}
    return {"limit": row.get("Лимит"), "spent": row.get("Истрачено")}


def _spent(payload: dict[str, Any], method: str) -> int | None:
    value = _usage(payload, method)["spent"]
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _print(label: str, value: dict[str, Any]) -> None:
    print(f"{label}={json.dumps(value, ensure_ascii=False)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Безопасная одиночная проверка API-ФНС: stat, один search, один egr, stat."
    )
    parser.add_argument("--okved", default="49.41", help="Основной ОКВЭД для search")
    parser.add_argument("--region", default="77", choices=("77", "50"), help="Код региона")
    parser.add_argument(
        "--stat-only",
        action="store_true",
        help="Показать расход search/egr одним запросом stat, без search и egr",
    )
    args = parser.parse_args()
    settings = get_settings()
    if not settings.api_fns_configured:
        parser.exit(1, "Ошибка: добавьте API_FNS_KEY только в локальный .env\n")

    failed = False
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    with ApiFnsClient(
        settings.api_fns_key,
        settings.api_fns_base_url,
        settings.api_fns_timeout_seconds,
        require_phone=True,
        require_email=True,
    ) as client:
        if args.stat_only:
            try:
                stats = client.get_statistics()
                _print(
                    "stat",
                    {"search": _usage(stats, "search"), "egr": _usage(stats, "egr")},
                )
            except ApiFnsAPIError as exc:
                _print("error", {"message": str(exc)})
                raise SystemExit(1) from exc
            return
        try:
            before = client.get_statistics()
            _print(
                "stat_before",
                {"search": _usage(before, "search"), "egr": _usage(before, "egr")},
            )
            page = client.search_by_okved(
                args.okved,
                region_code=args.region,
                page=1,
                limit=1,
            )
            first_inn = next(
                (str(item.get("ИНН") or "") for item in page.records if item.get("ИНН")),
                "",
            )
            _print(
                "search",
                {
                    "primary_okved": args.okved,
                    "region": args.region,
                    "requires_phone": True,
                    "requires_email": True,
                    "records_on_page": len(page.records),
                    "current_page": page.current_page,
                    "total_pages": page.total_pages,
                    "has_legal_entity": bool(first_inn),
                },
            )
            if not first_inn:
                raise ApiFnsAPIError(
                    "API-ФНС не вернул ЮЛ для выбранного ОКВЭД, региона и контактных фильтров."
                )
            company = client.get_company(first_inn)
            _print(
                "egr",
                {
                    "name": company.name,
                    "inn": company.inn,
                    "ogrn": company.ogrn,
                    "active": company.is_active,
                    "region_code": company.region_code,
                    "region_name": company.region_name,
                    "primary_okved": company.primary_okved.code if company.primary_okved else None,
                    "additional_okved_count": len(company.additional_okveds),
                    "phone_count": len(company.phone_numbers),
                    "email_count": len(company.emails),
                },
            )
        except (ApiFnsAPIError, ValueError) as exc:
            failed = True
            _print("error", {"message": str(exc)})
        finally:
            try:
                after = client.get_statistics()
                _print(
                    "stat_after",
                    {"search": _usage(after, "search"), "egr": _usage(after, "egr")},
                )
            except ApiFnsAPIError as exc:
                failed = True
                _print("stat_after_error", {"message": str(exc)})

    before_search = _spent(before, "search")
    after_search = _spent(after, "search")
    before_egr = _spent(before, "egr")
    after_egr = _spent(after, "egr")
    _print(
        "usage_delta",
        {
            "search": after_search - before_search
            if None not in (before_search, after_search)
            else None,
            "egr": after_egr - before_egr if None not in (before_egr, after_egr) else None,
        },
    )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
