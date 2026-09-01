from app.services.provider import CompanyPayload, OkvedItem


DEMO_COMPANIES = [
    CompanyPayload(
        name='ООО "ДЕМО ТРАНСЛОГИСТИК"',
        inn="7700000001",
        ogrn="1267700000001",
        primary_okved=OkvedItem("49.41", "Деятельность автомобильного грузового транспорта"),
        additional_okveds=[OkvedItem("52.21.2", "Вспомогательная деятельность, связанная с автотранспортом")],
        emails=["info@demo-translog.ru", "dispatch@demo-translog.ru"],
    ),
    CompanyPayload(
        name='ООО "ДЕМО ДОРОГИ"',
        inn="7700000002",
        ogrn="1267700000002",
        primary_okved=OkvedItem("42.11", "Строительство автомобильных дорог и автомагистралей"),
        additional_okveds=[OkvedItem("43.12.3", "Производство земляных работ")],
        emails=["office@demo-dorogi.ru"],
    ),
    CompanyPayload(
        name='ООО "ДЕМО АГРОПАРК"',
        inn="5000000003",
        ogrn="1265000000003",
        primary_okved=OkvedItem("01.11", "Выращивание зерновых культур"),
        additional_okveds=[OkvedItem("49.41.2", "Перевозка грузов неспециализированными автотранспортными средствами")],
        emails=[],
    ),
    CompanyPayload(
        name='ООО "ДЕМО СПЕЦТЕХ"',
        inn="5000000004",
        ogrn="1265000000004",
        primary_okved=OkvedItem("77.32", "Аренда строительных машин и оборудования"),
        additional_okveds=[OkvedItem("43.12", "Подготовка строительной площадки")],
        emails=["rent@demo-spectech.ru"],
    ),
    CompanyPayload(
        name='ООО "ДЕМО СТРОЙГРУПП"',
        inn="7800000005",
        ogrn="1267800000005",
        primary_okved=OkvedItem("41.20", "Строительство жилых и нежилых зданий"),
        additional_okveds=[OkvedItem("49.41.3", "Аренда грузового транспорта с водителем")],
        emails=["mail@demo-stroygroup.ru"],
    ),
    CompanyPayload(
        name='ООО "ДЕМО КАРЬЕР"',
        inn="7100000006",
        ogrn="1267100000006",
        primary_okved=OkvedItem("43.12.3", "Производство земляных работ"),
        additional_okveds=[OkvedItem("77.39.1", "Аренда сухопутных транспортных средств")],
        emails=[],
    ),
]
