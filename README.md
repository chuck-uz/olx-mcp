# uzParser

MCP-сервер, который даёт Claude инструменты поиска по OLX.uz, Uzum Market и avtoelon.uz. На просьбу «найди на OLX…»,
«найди на Uzum…» или «найди на avtoelon…» Claude не ходит на сайт сам (сайты режут такие запросы), а вызывает парсеры
и получает структурированные данные: цены в сумах и у.е., скидки, рейтинг, состояние, пробег, продавца.

```
Claude ──stdio──▶ uzParser (этот репозиторий, у вас на машине)
                     ├── OLX, Uzum: HTTPS + токен ──▶ сервер парсеров ──▶ headless Chromium ──▶ olx.uz / uzum.uz
                     └── avtoelon: напрямую с вашей машины ──▶ avtoelon.uz
```

Сам uzParser без зависимостей: стандартная библиотека Python 3.9+. Парсеры OLX и Uzum, очередь браузера и история
выгрузок живут на сервере ([chuck-uz/Tools](https://github.com/chuck-uz/Tools)); avtoelon парсится локально.

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

**OLX.** `sort`: `new` — сначала новые, `cheap` — дешёвые, `expensive` — дорогие; цены фильтра — в сумах.
Продвигаемые («топ») объявления OLX ставит первыми при любой сортировке, в ответе они помечены `promoted`.
OLX отдаёт не больше 1000 объявлений на один поиск. При `limit` больше 1000 (до 10 000) парсер сам дробит поиск
по цене: первые 1000 идут в порядке OLX, остальные от дешёвых к дорогим, объявления без цены в добор не попадают.
Около 1000 объявлений за 40 секунд.

При подключении сервер передаёт Claude инструкцию: всё про OLX, Uzum и машины делать через эти инструменты,
а не через веб-поиск. OLX — частные объявления и б/у, Uzum — новые товары магазинов, avtoelon — машины.

### avtoelon работает с вашего компьютера

avtoelon.uz показывает объявления только узбекским IP, поэтому инструменты `avtoelon_*` ходят на сайт напрямую
с машины, где запущен uzParser: токен и сервер не нужны, но нужен узбекский адрес (без VPN). Сайт отдаёт обычный
HTML, браузер не нужен; пауза 1 с между страницами (20 объявлений). Выгрузки сохраняются локально в
`~/.uzparser/avtoelon/` (другая папка — переменная `UZPARSER_DATA`).

Объявления с полем `rent` («Аренда N y.e./мес») — аренда или лизинг: их цена в объявлении не рыночная.

## Установка

Сервер парсеров личный: для OLX и Uzum нужен токен от его владельца (выдаётся через Telegram-бота вместе с готовой
инструкцией). avtoelon работает и без токена.

1. **Код.**

   ```bash
   git clone https://github.com/chuck-uz/uzparser ~/uzparser
   ```

2. **Claude Code** (для всех проектов):

   ```bash
   claude mcp add uzparser -s user -e UZPARSER_TOKEN=tools_… -- python3 ~/uzparser/uzparser/server.py
   ```

   **Claude Desktop** — в `~/Library/Application Support/Claude/claude_desktop_config.json`, затем перезапустить приложение:

   ```json
   {"mcpServers": {"uzparser": {"command": "python3",
     "args": ["/Users/<вы>/uzparser/uzparser/server.py"],
     "env": {"UZPARSER_TOKEN": "tools_…"}}}}
   ```

Переменные: `UZPARSER_TOKEN` (для OLX и Uzum), `UZPARSER_URL` (адрес сервера парсеров, по умолчанию — сервер
владельца), `UZPARSER_DATA` (папка выгрузок avtoelon).

### Переход со старого имени (olx-mcp)

Проект раньше назывался `olx-mcp`. Старые настройки продолжают работать: GitHub перенаправляет старый адрес
репозитория, `olx_mcp/server.py` запускает тот же сервер, `OLX_MCP_TOKEN` / `OLX_MCP_URL` / `OLX_MCP_DATA`
понимаются, а `~/.olx-mcp` при первом запуске переезжает в `~/.uzparser`. Чтобы перейти на новое имя в Claude Code:

```bash
claude mcp remove olx -s user
claude mcp add uzparser -s user -e UZPARSER_TOKEN=tools_… -- python3 ~/olx-mcp/uzparser/server.py
```

## Разработка

```bash
python3 -m unittest -v
```

Проверка вручную, без Claude:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"olx_list_dumps","arguments":{}}}' \
  | UZPARSER_TOKEN=tools_… python3 uzparser/server.py
```

## Дальше

- Режим `local` для OLX и Uzum: Playwright прямо на машине, без сервера.
- Доступ из claude.ai и мобильного приложения: удалённый MCP-эндпоинт с OAuth.
