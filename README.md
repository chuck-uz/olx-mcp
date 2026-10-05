# olx-mcp

MCP-сервер, который даёт Claude инструменты поиска по OLX.uz, Uzum Market и avtoelon.uz. На просьбу «найди на OLX…» или
«найди на Uzum…» Claude не ходит на сайт сам (оба сайта режут такие запросы), а вызывает парсеры на
[tools.oresh.in](https://tools.oresh.in) и получает структурированные данные: цены в сумах, скидки, рейтинг,
состояние, продавца.

```
Claude ──stdio──▶ olx-mcp (этот репозиторий, у вас на машине)
                     │ HTTPS + Bearer-токен
                     ▼
               tools.oresh.in/api/olx ──▶ headless Chromium ──▶ olx.uz
```

Сам сервер ничего не парсит и не имеет зависимостей: один файл на стандартной библиотеке Python 3.9+.
Парсер, очередь и история выгрузок живут в [chuck-uz/Tools](https://github.com/chuck-uz/Tools).

## Инструменты

| Инструмент | Что делает |
|---|---|
| `olx_search(query, limit, price_from, price_to, sort, photos)` | Собирает объявления по запросу («iphone 15») или по ссылке на список olx.uz с фильтрами. Возвращает сводку (мин/медиана/макс цены, сколько магазинов) и первые 50 объявлений |
| `olx_get_offers(dump_id, offset, count, full)` | Дочитывает выгрузку порциями; `full` — полные описания и все параметры |
| `olx_list_dumps(limit)` | Прошлые выгрузки OLX: запрос, дата, медиана цены |
| `uzum_search(query, limit, sort, photos)` | Товары Uzum Market по запросу или ссылке на поиск/категорию: цена, цена по карте Uzum, скидка, рассрочка, рейтинг, доставка. `sort`: `popular`, `cheap`, `expensive`, `rating`, `new`. До 10 000 |
| `uzum_get_items(dump_id, offset, count)` | Дочитывает выгрузку Uzum порциями |
| `uzum_list_dumps(limit)` | Прошлые выгрузки Uzum |
| `avtoelon_search(query, limit, price_from, price_to, year_from, year_to, sort)` | Машины на avtoelon.uz: цена в у.е., год, пробег, двигатель, топливо, КПП, кузов, город, «торг», аренда. `query` — марка и модель латиницей («chevrolet cobalt») или ссылка с фильтрами. Цены фильтра — в у.е. `sort`: `new`, `cheap`, `expensive`, `year_new`, `year_old`. До 5000 |
| `avtoelon_get_items(dump_id, offset, count, full)` | Дочитывает выгрузку avtoelon |
| `avtoelon_list_dumps(limit)` | Прошлые выгрузки avtoelon |

`sort`: `new` — сначала новые, `cheap` — дешёвые, `expensive` — дорогие. Цены фильтра — в сумах.
Продвигаемые («топ») объявления OLX ставит первыми при любой сортировке, в ответе они помечены `promoted`.
OLX отдаёт не больше 1000 объявлений на один поиск. При `limit` больше 1000 (до 10 000) парсер сам дробит поиск
по цене: первые 1000 идут в порядке OLX, остальные от дешёвых к дорогим, объявления без цены в добор не попадают.
Скорость около 1000 объявлений за 40 секунд.

При подключении сервер передаёт Claude инструкцию: всё, что касается OLX.uz и Uzum Market, делать через эти
инструменты, а не через веб-поиск. OLX — частные объявления и б/у, Uzum — новые товары магазинов; для оценки
рынка Claude может собрать с обеих площадок.

### avtoelon работает с вашего компьютера

avtoelon.uz показывает объявления только узбекским IP: с сервера tools.oresh.in (США) он отвечает 404. Поэтому
инструменты `avtoelon_*` ходят на сайт напрямую с машины, где запущен MCP-сервер. Им не нужен токен и сервер,
но нужен узбекский адрес. Сайт отдаёт обычный HTML, браузер не нужен; пауза 1 с между страницами (20 объявлений).
Выгрузки сохраняются локально в `~/.olx-mcp/avtoelon/` (другая папка — переменная `OLX_MCP_DATA`).

Объявления с полем `rent` («Аренда N y.e./мес») — аренда или лизинг: их цена в объявлении не рыночная.

## Установка

Сервер tools.oresh.in личный: для работы нужен токен от его владельца. Если токен вам уже выдали,
пропустите шаг 1.

1. **Токен** (делает владелец сервера). На сервере tools создать именной токен и добавить строку `имя:хэш` в `API_TOKEN_HASHES` (см. README Tools, раздел
   «Вход и токены»), перезапустить контейнер:

   ```bash
   ssh oresh 'sudo docker exec tools python -m app.auth new-token <имя>'
   ```

2. **Код.**

   ```bash
   git clone git@github.com:chuck-uz/olx-mcp.git ~/Claude/Projects/olx-mcp
   ```

3. **Claude Code** (для всех проектов):

   ```bash
   claude mcp add olx -s user -e OLX_MCP_TOKEN=tools_… -- python3 ~/Claude/Projects/olx-mcp/olx_mcp/server.py
   ```

   **Claude Desktop** — в `~/Library/Application Support/Claude/claude_desktop_config.json`, затем перезапустить приложение:

   ```json
   {"mcpServers": {"olx": {"command": "/usr/bin/python3",
     "args": ["/Users/<вы>/Claude/Projects/olx-mcp/olx_mcp/server.py"],
     "env": {"OLX_MCP_TOKEN": "tools_…"}}}}
   ```

Переменные: `OLX_MCP_TOKEN` (обязательна), `OLX_MCP_URL` (по умолчанию `https://tools.oresh.in`).
Отозвать доступ — убрать хэш токена из `API_TOKEN_HASHES` на сервере.

## Разработка

```bash
python3 -m unittest -v
```

Проверка вручную, без Claude:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"olx_list_dumps","arguments":{}}}' \
  | OLX_MCP_TOKEN=tools_… python3 olx_mcp/server.py
```

## Дальше

- Режим `local`: Playwright прямо на машине, без сервера.
- Доступ из claude.ai и мобильного приложения: удалённый MCP-эндпоинт на tools.oresh.in с OAuth.
